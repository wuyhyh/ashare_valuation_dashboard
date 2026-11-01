"""Explicit post-close spot-to-independent-daily cross-check for v0.11.

This module does not manufacture historical breadth. It can only verify a
previously captured, same-session, full-market spot snapshot with a second
provider's actual *dated*, unadjusted individual stock daily closes.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time, timezone
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
    "sina": ("ak.stock_zh_a_daily", "date", "close"),
    "eastmoney": ("ak.stock_zh_a_hist", "日期", "收盘"),
}
SAMPLES_PER_BOARD = 5


class CloseVerificationError(ValueError):
    pass


def _reference_worker(codes: list[tuple[str, str]], day: str, provider: str, out) -> None:
    try:
        import akshare as ak
        values = {}
        for code, exchange in codes:
            if provider == "sina":
                frame = ak.stock_zh_a_daily(
                    symbol=("sh" if exchange == "SH" else "sz") + code,
                    start_date=day.replace("-", ""), end_date=day.replace("-", ""), adjust="",
                )
            else:
                frame = ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=day.replace("-", ""), end_date=day.replace("-", ""), adjust="",
                )
            date_key, close_key = REFERENCE_PROVIDERS[provider][1:]
            if frame is not None and not frame.empty and {date_key, close_key}.issubset(frame.columns):
                rows = frame.to_dict(orient="records")
                values[code] = [
                    {"date": str(r[date_key])[:10], "close": float(r[close_key])}
                    for r in rows
                ]
            else:
                values[code] = []
            clock.sleep(0.75)  # conservative request spacing to limit provider pressure
        out.put({"ok": True, "rows": values, "akshare_version": getattr(ak, "__version__", "unknown")})
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
        raise CloseVerificationError("独立日线核验失败：" + str(result.get("error")))
    return result


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
    reference_provider: str = "sina", fetcher=None,
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
        matches.append({
            "code": row["code"], "board": row["board"],
            "spot_close": float(row["price"]), "independent_close": close,
            "relative_diff": difference,
        })
    evidence = {
        "status": "passed",
        "method": EVIDENCE_METHOD,
        "calculation_version": HISTORY_CALCULATION_VERSION,
        "reference_source": REFERENCE_PROVIDERS[reference_provider][0],
        "reference_provider": reference_provider,
        "reference_akshare_version": str(reference.get("akshare_version", "unknown")),
        "reference_trading_date": day,
        "checked_samples": len(sampled),
        "matched_samples": len(matches),
        "max_relative_diff": max(match["relative_diff"] for match in matches),
        "checked_boards": list(SH_SZ_BOARDS),
        "sample_audit": matches,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    }
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
        "warning": "仅对已采集快照的沪深四板块进行20支独立收盘日线抽样核验；北交所未核验，不可进入BSE/ALL_A历史",
    }
