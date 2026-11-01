"""Explicit post-close spot-to-independent-daily cross-check for v0.11.

This module does not manufacture historical breadth. It can only verify a
previously captured, same-session, full-market spot snapshot with a second
provider's actual *dated*, unadjusted individual stock daily closes.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import multiprocessing
from pathlib import Path
import queue
import sqlite3
import time as clock

from breadth import CHINA_TZ, SH_SZ_BOARDS
from breadth_history import EVIDENCE_METHOD, HISTORY_CALCULATION_VERSION, MAX_RELATIVE_DIFF

REFERENCE_PROVIDERS = {
    "sina": {
        "interface": "ak.stock_zh_a_daily", "date_field": "date", "close_field": "close",
        "adjustment": "none", "symbol_format": "exchange-prefixed",
    },
    "eastmoney": {
        "interface": "ak.stock_zh_a_hist", "date_field": "日期", "close_field": "收盘",
        "adjustment": "none", "symbol_format": "six-digit",
    },
}
SAMPLES_PER_BOARD = 5
MAX_CHANGE_DIFF_PERCENTAGE_POINTS = 0.02


class CloseVerificationError(ValueError):
    pass


def _reference_worker(codes: list[tuple[str, str]], day: str, provider: str, out) -> None:
    try:
        import akshare as ak
        values = {}
        diagnostics = {}
        errors = []
        requested_day = date.fromisoformat(day)
        start_day = (requested_day - timedelta(days=10)).strftime("%Y%m%d")
        end_day = requested_day.strftime("%Y%m%d")
        provider_info = REFERENCE_PROVIDERS[provider]
        for code, exchange in codes:
            started = clock.monotonic()
            try:
                if provider == "sina":
                    frame = ak.stock_zh_a_daily(
                        symbol=("sh" if exchange == "SH" else "sz") + code,
                        start_date=start_day, end_date=end_day, adjust="",
                    )
                else:
                    frame = ak.stock_zh_a_hist(
                        symbol=code, period="daily",
                        start_date=start_day, end_date=end_day, adjust="",
                    )
                date_key = provider_info["date_field"]
                close_key = provider_info["close_field"]
                columns = [] if frame is None else list(frame.columns)
                diagnostics[code] = {
                    "exchange": exchange, "columns": columns,
                    "elapsed_seconds": round(clock.monotonic() - started, 3),
                    "row_count": 0 if frame is None else len(frame),
                }
                if frame is None or frame.empty or not {date_key, close_key}.issubset(columns):
                    values[code] = []
                    errors.append({
                        "code": code,
                        "error": "empty response" if frame is None or frame.empty else "required fields missing",
                        "columns": columns,
                    })
                else:
                    values[code] = [
                        {"date": str(r[date_key])[:10], "close": float(r[close_key])}
                        for r in frame.to_dict(orient="records")
                    ]
            except Exception as exc:
                values[code] = []
                diagnostics[code] = {
                    "exchange": exchange, "columns": [],
                    "elapsed_seconds": round(clock.monotonic() - started, 3), "row_count": 0,
                }
                errors.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
                if type(exc).__name__ == "Push2RateLimitError":
                    break
            clock.sleep(0.75)  # conservative request spacing to limit provider pressure
        common_columns = sorted({column for item in diagnostics.values() for column in item["columns"]})
        out.put({
            "ok": not errors, "rows": values, "errors": errors,
            "akshare_version": getattr(ak, "__version__", "unknown"),
            "reference_interface": provider_info["interface"],
            "adjustment": provider_info["adjustment"],
            "symbol_format": provider_info["symbol_format"],
            "date_field": provider_info["date_field"], "close_field": provider_info["close_field"],
            "request_start_date": (requested_day - timedelta(days=10)).isoformat(),
            "request_end_date": requested_day.isoformat(),
            "native_columns": common_columns, "request_diagnostics": diagnostics,
        })
    except Exception as exc:
        out.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def fetch_independent_daily(
    codes: list[tuple[str, str]], day: str, provider: str, timeout_seconds: int = 150,
) -> dict:
    if provider not in REFERENCE_PROVIDERS:
        raise CloseVerificationError("未知独立日线来源")
    if not 0 < timeout_seconds <= 300:
        raise CloseVerificationError("核验超时必须在1—300秒之间")
    context = multiprocessing.get_context("spawn")
    messages = context.Queue(maxsize=1)
    process = context.Process(
        target=_reference_worker, args=(codes, day, provider, messages), daemon=True,
    )
    process.start()
    try:
        try:
            result = messages.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise CloseVerificationError("独立日线核验请求超时，未修改本地快照") from exc
    finally:
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        messages.close()
    if not result.get("ok"):
        errors = result.get("errors") or []
        detail = json.dumps(errors[:3], ensure_ascii=False) if errors else str(result.get("error"))
        raise CloseVerificationError("独立日线核验失败：" + detail)
    return result


def diagnose_reference_source(
    db_path: str | Path, *, day: str, reference_provider: str,
    capture_id: str | None = None, timeout_seconds: int = 150,
) -> dict:
    """Probe a dated reference source without comparing or promoting a capture.

    The sample identity comes from the selected current capture only to exercise
    the interfaces. It must never be treated as a point-in-time historical pool.
    """
    try:
        requested_day = date.fromisoformat(day)
    except (TypeError, ValueError) as exc:
        raise CloseVerificationError("诊断日期必须是 YYYY-MM-DD") from exc
    local_now = datetime.now(CHINA_TZ)
    if (requested_day.weekday() >= 5 or requested_day > local_now.date()
            or (requested_day == local_now.date() and local_now.time() < time(15, 30))):
        raise CloseVerificationError("参考源诊断日期必须是已结束的沪深工作日")
    path = Path(db_path)
    if not path.is_file():
        raise CloseVerificationError("本地尚无宽度数据库")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        capture = db.execute(
            "SELECT * FROM breadth_captures WHERE capture_id=?" if capture_id else
            "SELECT * FROM breadth_captures ORDER BY captured_at_utc DESC LIMIT 1",
            (capture_id,) if capture_id else (),
        ).fetchone()
        if capture is None:
            raise CloseVerificationError("未找到用于选取诊断样本的快照")
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM breadth_quotes WHERE capture_id=?", (capture["capture_id"],),
        )]
        sampled = _choose_samples(rows)
    started = clock.monotonic()
    reference = fetch_independent_daily(
        [(row["code"], row["exchange"]) for row in sampled], day,
        reference_provider, timeout_seconds=timeout_seconds,
    )
    target_rows = {}
    previous_rows = {}
    for row in sampled:
        records = sorted(reference["rows"].get(row["code"]) or [], key=lambda item: item["date"])
        target = [item for item in records if item["date"] == day]
        previous = [item for item in records if item["date"] < day]
        target_rows[row["code"]] = target[0] if len(target) == 1 else None
        previous_rows[row["code"]] = previous[-1] if previous else None
    return {
        "status": (
            "reference_source_usable"
            if all(target_rows.values()) and all(previous_rows.values())
            else "reference_source_incomplete"
        ),
        "reference_provider": reference_provider,
        "reference_interface": reference["reference_interface"],
        "akshare_version": str(reference.get("akshare_version", "unknown")),
        "requested_trading_date": day, "requested_samples": len(sampled),
        "target_rows": sum(value is not None for value in target_rows.values()),
        "previous_close_rows": sum(value is not None for value in previous_rows.values()),
        "elapsed_seconds": round(clock.monotonic() - started, 3),
        "adjustment": reference["adjustment"], "symbol_format": reference["symbol_format"],
        "date_field": reference["date_field"], "close_field": reference["close_field"],
        "native_columns": reference["native_columns"],
        "request_start_date": reference["request_start_date"],
        "request_end_date": reference["request_end_date"],
        "sample_codes": [
            {"code": row["code"], "exchange": row["exchange"], "board": row["board"],
             "target": target_rows[row["code"]], "previous": previous_rows[row["code"]]}
            for row in sampled
        ],
        "written": False,
        "warning": (
            "仅诊断独立日线接口；样本身份来自当前快照，不能证明历史时点股票池，"
            "不能升级任何宽度快照或构造历史曲线"
        ),
    }


def _choose_samples(rows: list[dict]) -> list[dict]:
    chosen = []
    for board in SH_SZ_BOARDS:
        board_rows = sorted(
            (r for r in rows if r["board"] == board
             and r["security_type"] == "A_SHARE" and r["traded"]
             and r["price"] is not None and r["price"] > 0),
            key=lambda r: r["code"],
        )
        if len(board_rows) < SAMPLES_PER_BOARD:
            raise CloseVerificationError(f"{board} 可核验有效成交股票不足{SAMPLES_PER_BOARD}只")
        selected = [
            board_rows[min(int((i + .5) * len(board_rows) / SAMPLES_PER_BOARD),
                           len(board_rows) - 1)]
            for i in range(SAMPLES_PER_BOARD)
        ]
        if len({r["code"] for r in selected}) != SAMPLES_PER_BOARD:
            raise CloseVerificationError(f"{board} 独立样本有重复代码")
        chosen.extend(selected)
    return chosen


def verify_close(
    db_path: str | Path, *, capture_id: str | None = None,
    reference_provider: str = "sina", fetcher=None, dry_run: bool = False,
) -> dict:
    """Verify an existing spot capture; commit evidence only after 20/20 passes.

    `fetcher` is only for offline contract tests. No test fixture is persisted
    to a real database by the production command.
    """
    if reference_provider not in REFERENCE_PROVIDERS:
        raise CloseVerificationError("独立来源只支持 sina / eastmoney")
    path = Path(db_path)
    if not path.is_file():
        raise CloseVerificationError("本地尚无宽度数据库")
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        if capture_id is not None:
            row = db.execute(
                "SELECT * FROM breadth_captures WHERE capture_id=?", (capture_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM breadth_captures ORDER BY captured_at_utc DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise CloseVerificationError("未找到待核验的快照")
        capture = dict(row)
        quality = json.loads(capture["quality_json"])
        if capture["coverage_scope"] not in ("sh_sz_all_a", "sh_sz_all_a_with_bse"):
            raise CloseVerificationError("不允许将v0.9旧主板快照升级为全市场核验")
        if quality.get("provider") == reference_provider or quality.get("provider") not in ("tencent", "sina", "eastmoney"):
            raise CloseVerificationError("行情源与独立日线参考源必须不同，且来源可验证")
        day = capture["capture_local_date"]
        try:
            trading_day = date.fromisoformat(day)
            instant = datetime.fromisoformat(capture["captured_at_utc"].replace("Z", "+00:00"))
            local = instant.astimezone(CHINA_TZ)
        except (ValueError, TypeError) as exc:
            raise CloseVerificationError("快照采集日期无效") from exc
        if (instant.tzinfo is None or local.date() != trading_day or trading_day.weekday() >= 5
                or local.time() < time(15, 30)):
            raise CloseVerificationError("只允许同一交易日北京时间15:30之后捕获的快照")
        official = (quality.get("official_pool") or {}).get("boards") or {}
        if any(float((official.get(board) or {}).get("coverage_ratio", 0)) < .95 for board in SH_SZ_BOARDS):
            raise CloseVerificationError("逐板交易所官方名录覆盖不足，不能建立可信历史")
        quotes = [dict(r) for r in db.execute(
            "SELECT * FROM breadth_quotes WHERE capture_id=?", (capture["capture_id"],),
        )]
        sampled = _choose_samples(quotes)
    codes = [(r["code"], r["exchange"]) for r in sampled]
    reference = (
        fetcher(codes, day, reference_provider) if fetcher is not None
        else fetch_independent_daily(codes, day, reference_provider)
    )
    if not isinstance(reference, dict) or not reference.get("ok"):
        raise CloseVerificationError("独立日线返回失败，未修改快照")
    raw = reference.get("rows") or {}
    matches = []
    for row in sampled:
        records = raw.get(row["code"]) or []
        dated = [r for r in records if str(r.get("date"))[:10] == day]
        if len(dated) != 1:
            raise CloseVerificationError(f"{row['code']} 没有唯一的 {day} 日线收盘记录")
        try:
            close = float(dated[0]["close"])
        except (ValueError, TypeError, KeyError) as exc:
            raise CloseVerificationError(f"{row['code']} 日线收盘字段无效") from exc
        if not math.isfinite(close) or close <= 0:
            raise CloseVerificationError(f"{row['code']} 日线收盘不是有效正数")
        difference = abs(float(row["price"]) - close) / close
        if difference > MAX_RELATIVE_DIFF:
            raise CloseVerificationError(
                f"{row['code']} 收盘价差 {difference:.4%} 超过阈值，快照不作为收盘历史"
            )
        previous = sorted(
            (r for r in records if str(r.get("date"))[:10] < day),
            key=lambda item: str(item.get("date"))[:10],
        )
        if not previous:
            raise CloseVerificationError(f"{row['code']} 缺少 {day} 之前的未复权收盘，无法核对前收盘")
        try:
            previous_close = float(previous[-1]["close"])
        except (ValueError, TypeError, KeyError) as exc:
            raise CloseVerificationError(f"{row['code']} 前收盘字段无效") from exc
        if not math.isfinite(previous_close) or previous_close <= 0:
            raise CloseVerificationError(f"{row['code']} 前收盘不是有效正数")
        reference_change = (close / previous_close - 1) * 100
        try:
            spot_change = float(row["change_pct"])
        except (ValueError, TypeError, KeyError) as exc:
            raise CloseVerificationError(f"{row['code']} 快照涨跌幅字段无效") from exc
        change_difference = abs(spot_change - reference_change)
        if not math.isfinite(spot_change) or change_difference > MAX_CHANGE_DIFF_PERCENTAGE_POINTS:
            raise CloseVerificationError(
                f"{row['code']} 快照涨跌幅与未复权前收盘推导值相差 "
                f"{change_difference:.4f} 个百分点"
            )
        matches.append({
            "code": row["code"], "board": row["board"],
            "spot_close": float(row["price"]), "independent_close": close,
            "relative_diff": difference,
            "previous_trading_date": str(previous[-1]["date"])[:10],
            "previous_close": previous_close, "reference_change_pct": reference_change,
            "spot_change_pct": spot_change,
            "change_pct_diff": change_difference,
        })
    provider_info = REFERENCE_PROVIDERS[reference_provider]
    evidence = {
        "status": "passed",
        "method": EVIDENCE_METHOD,
        "calculation_version": HISTORY_CALCULATION_VERSION,
        "reference_source": provider_info["interface"],
        "reference_provider": reference_provider,
        "reference_akshare_version": str(reference.get("akshare_version", "unknown")),
        "reference_adjustment": str(reference.get("adjustment", provider_info["adjustment"])),
        "reference_date_field": str(reference.get("date_field", provider_info["date_field"])),
        "reference_close_field": str(reference.get("close_field", provider_info["close_field"])),
        "reference_native_columns": list(reference.get("native_columns") or []),
        "request_start_date": reference.get("request_start_date"),
        "request_end_date": reference.get("request_end_date"),
        "reference_trading_date": day,
        "checked_samples": len(sampled),
        "matched_samples": len(matches),
        "max_relative_diff": max(match["relative_diff"] for match in matches),
        "max_change_diff_percentage_points": max(match["change_pct_diff"] for match in matches),
        "checked_boards": list(SH_SZ_BOARDS),
        "sample_strategy": "five_code_quantiles_per_board_from_valid_traded_a_shares",
        "samples_per_board": SAMPLES_PER_BOARD,
        "sample_audit": matches,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if dry_run:
        return {
            "capture_id": capture["capture_id"], "trading_date": day,
            "reference_source": evidence["reference_source"],
            "matched_samples": len(matches),
            "max_relative_diff": evidence["max_relative_diff"],
            "dry_run": True,
            "written": False,
            "warning": "核验已计算但未写入；正式收盘日状态仍未激活",
        }
    # Back up the *complete* SQLite database through its backup API, not a
    # raw copy of a possibly live WAL-mode file.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(path.name + ".backup-before-close-verification-" + stamp)
    with closing(sqlite3.connect(path)) as original, closing(sqlite3.connect(backup)) as target:
        original.backup(target)
    # Transactional re-read prevents replacing a capture changed while the
    # independent network requests were in flight.
    with closing(sqlite3.connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT quality_json,trading_date_verified FROM breadth_captures WHERE capture_id=?",
            (capture["capture_id"],),
        ).fetchone()
        if current is None or current[0] != capture["quality_json"] or current[1] != capture["trading_date_verified"]:
            db.rollback()
            raise CloseVerificationError("核验期间快照记录已改变，未写入")
        quality["close_verification"] = evidence
        db.execute(
            "UPDATE breadth_captures SET trading_date=?,trading_date_verified=1,"
            "quote_date_note=?,quality_json=? WHERE capture_id=?",
            (day, "同一收盘日逐股独立日线交叉核验通过（仅所验证的沪深市场板块）；"
                  "该核验不证明全部证券每一个成交价准确",
             json.dumps(quality, ensure_ascii=False), capture["capture_id"]),
        )
        db.commit()
    return {
        "capture_id": capture["capture_id"],
        "trading_date": day, "reference_source": evidence["reference_source"],
        "matched_samples": len(matches), "max_relative_diff": evidence["max_relative_diff"],
        "dry_run": False, "written": True, "backup": str(backup),
        "warning": "仅对已采集快照的沪深四板块进行20支独立收盘日线抽样核验；北交所未核验，不可进入BSE/ALL_A历史",
    }
