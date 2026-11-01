"""Point-in-time Shanghai/Shenzhen main-board market breadth snapshots.

An intraday quote snapshot does NOT prove its exchange trade date. Snapshot
capture times are distinct from exchange trading dates; no historical backfill
is manufactured. This module never fetches data unless explicitly requested.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import json
import math
from pathlib import Path
import multiprocessing
import queue
import re
import sqlite3
from statistics import median
import uuid
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "breadth.sqlite3"
CHINA_TZ = ZoneInfo("Asia/Shanghai")
SH_PREFIXES = ("600", "601", "603", "605")
SZ_PREFIXES = ("000", "001", "002", "003")
REQUIRED_COLUMNS = ("代码", "名称", "最新价", "涨跌幅", "成交额")


class BreadthError(ValueError):
    pass


def mainboard_exchange(code: str) -> str | None:
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    if code.startswith(SH_PREFIXES):
        return "SH"
    if code.startswith(SZ_PREFIXES):
        return "SZ"
    return None


def risk_warning(name: str) -> bool:
    label = str(name).strip().upper().replace(" ", "")
    return bool(re.match(r"^(?:\*ST|ST|S\*ST|SST|退)", label))


def finite_float(value) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(str(value).strip().replace(",", "").replace("%", ""))
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_quotes(records: list[dict]) -> tuple[list[dict], dict]:
    if not isinstance(records, list) or not records:
        raise BreadthError("上游快照为空，不能生成市场宽度")
    if not REQUIRED_COLUMNS or not set(REQUIRED_COLUMNS).issubset(set(records[0])):
        raise BreadthError("上游字段缺失（需要代码、名称、最新价、涨跌幅、成交额）")
    quotes = []
    seen = set()
    excluded = {"not_mainboard": 0, "bad_code": 0, "duplicate_code": 0}
    for raw in records:
        code = str(raw.get("代码", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            excluded["bad_code"] += 1
            continue
        exchange = mainboard_exchange(code)
        if exchange is None:
            excluded["not_mainboard"] += 1
            continue
        if code in seen:
            # Ambiguous repeat: don't silently count twice or choose a winner.
            raise BreadthError(f"同一抓取结果出现重复股票代码 {code}；已拒绝快照")
        seen.add(code)
        name = str(raw.get("名称") or "").strip()
        price = finite_float(raw.get("最新价"))
        change = finite_float(raw.get("涨跌幅"))
        turnover = finite_float(raw.get("成交额"))
        quoted = bool(
            name and price is not None and price > 0
            and change is not None and turnover is not None and turnover >= 0
        )
        # A non-trading share is not a flat-return share.
        traded = quoted and turnover > 0
        quotes.append({
            "code": code, "name": name, "exchange": exchange,
            "risk_warning": risk_warning(name),
            "price": price, "change_pct": change, "turnover": turnover,
            "valid_quote": quoted, "traded": traded,
        })
    return quotes, {"raw_count": len(records), "identified_mainboard": len(quotes), **excluded}


def summarize(quotes: list[dict], *, exchange: str = "ALL", exclude_risk: bool = False) -> dict:
    if exchange not in ("ALL", "SH", "SZ"):
        raise BreadthError(f"不支持交易所: {exchange}")
    pool = [row for row in quotes if exchange == "ALL" or row["exchange"] == exchange]
    without_risk = [row for row in pool if not row["risk_warning"]]
    selected = without_risk if exclude_risk else pool
    active = [row for row in selected if row["traded"]]
    advances = sum(row["change_pct"] > 0 for row in active)
    declines = sum(row["change_pct"] < 0 for row in active)
    unchanged = len(active) - advances - declines
    turnover_total = sum(row["turnover"] for row in active)
    weighted = (
        sum(row["change_pct"] * row["turnover"] for row in active) / turnover_total
        if turnover_total > 0 else None
    )
    return {
        "exchange": exchange,
        "exclude_risk": exclude_risk,
        "identified_mainboard": len(pool),
        "risk_warning": len(pool) - len(without_risk),
        "excluded_risk": len(pool) - len(selected),
        "selected_count": len(selected),
        "valid_quote_count": sum(row["valid_quote"] for row in selected),
        "traded_count": len(active),
        "zero_turnover_count": sum(row["valid_quote"] and not row["traded"] for row in selected),
        "missing_quote_count": sum(not row["valid_quote"] for row in selected),
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "advance_ratio": advances / len(active) if active else None,
        "advance_decline_ratio": advances / declines if declines else None,
        "median_change_pct": median([row["change_pct"] for row in active]) if active else None,
        "turnover_weighted_change_pct": weighted,
        "turnover_yuan": turnover_total,
        "interpretation": "仅统计当前数据源返回、识别为主板且有效成交额大于0的样本；不是交易所全部挂牌证券的独立核验。",
    }


def create_capture(records: list[dict], *, source_interface: str, source_name: str,
                   version: str = "unknown", captured_at: datetime | None = None,
                   min_identified: int = 1000) -> dict:
    if not source_interface.strip() or not source_name.strip():
        raise BreadthError("必须提供数据来源与接口名称")
    if captured_at is None:
        captured_at = datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        raise BreadthError("采集时间必须包含时区")
    captured_at = captured_at.astimezone(timezone.utc)
    quotes, checks = normalize_quotes(records)
    if len(quotes) < min_identified:
        raise BreadthError(f"主板候选仅 {len(quotes)} 条（至少需要 {min_identified}）；可能上游分页缺失，拒绝保存")
    valid = sum(item["traded"] for item in quotes)
    if valid < max(1, int(len(quotes) * .70)):
        raise BreadthError(f"有效成交股票仅 {valid}/{len(quotes)}，可能行情字段异常；拒绝保存")
    return {
        "capture_id": uuid.uuid4().hex,
        "captured_at_utc": captured_at.isoformat(),
        "capture_local_date": captured_at.astimezone(CHINA_TZ).date().isoformat(),
        "trading_date": None,
        "trading_date_verified": False,
        "source_name": source_name, "source_interface": source_interface,
        "source_version": version,
        "quote_date_note": "上游股票实时行情快照未证明交易所交易日期；采集本地日期不能替代交易日。",
        "quality": checks,
        "quotes": quotes,
    }


def open_db(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS breadth_captures (
        capture_id TEXT PRIMARY KEY,
        captured_at_utc TEXT NOT NULL,
        capture_local_date TEXT NOT NULL,
        trading_date TEXT,
        trading_date_verified INTEGER NOT NULL DEFAULT 0,
        source_name TEXT NOT NULL,
        source_interface TEXT NOT NULL,
        source_version TEXT NOT NULL,
        quote_date_note TEXT NOT NULL,
        quality_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS breadth_quotes (
        capture_id TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        exchange TEXT NOT NULL,
        risk_warning INTEGER NOT NULL,
        price REAL,
        change_pct REAL,
        turnover REAL,
        valid_quote INTEGER NOT NULL,
        traded INTEGER NOT NULL,
        PRIMARY KEY (capture_id, code),
        FOREIGN KEY (capture_id) REFERENCES breadth_captures(capture_id)
    );
    CREATE INDEX IF NOT EXISTS breadth_captured_idx ON breadth_captures(captured_at_utc DESC);
    """)
    return connection


def persist_capture(capture: dict, db_path: str | Path = DEFAULT_DB) -> str:
    with closing(open_db(db_path)) as db, db:
        db.execute("""
            INSERT INTO breadth_captures (
                capture_id,captured_at_utc,capture_local_date,trading_date,
                trading_date_verified,source_name,source_interface,source_version,
                quote_date_note,quality_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            capture["capture_id"], capture["captured_at_utc"], capture["capture_local_date"],
            capture["trading_date"], int(capture["trading_date_verified"]),
            capture["source_name"], capture["source_interface"], capture["source_version"],
            capture["quote_date_note"], json.dumps(capture["quality"], ensure_ascii=False)
        ))
        db.executemany("""
            INSERT INTO breadth_quotes (
                capture_id,code,name,exchange,risk_warning,price,change_pct,turnover,valid_quote,traded
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, [
            (capture["capture_id"], q["code"], q["name"], q["exchange"], int(q["risk_warning"]),
             q["price"], q["change_pct"], q["turnover"], int(q["valid_quote"]), int(q["traded"]))
            for q in capture["quotes"]
        ])
    return capture["capture_id"]


def recent_captures(db_path: str | Path = DEFAULT_DB, limit: int = 20) -> list[dict]:
    if not Path(db_path).exists():
        return []
    with closing(open_db(db_path)) as db, db:
        rows = db.execute(
            "SELECT * FROM breadth_captures ORDER BY captured_at_utc DESC, capture_id DESC LIMIT ?",
            (min(max(int(limit), 1), 100),),
        ).fetchall()
    return [dict(row) for row in rows]


def latest_report(db_path: str | Path = DEFAULT_DB) -> dict:
    if not Path(db_path).exists():
        return {"available": False, "message": "尚未采集真实主板宽度快照", "history": []}
    with closing(open_db(db_path)) as db, db:
        capture = db.execute(
            "SELECT * FROM breadth_captures ORDER BY captured_at_utc DESC, capture_id DESC LIMIT 1"
        ).fetchone()
        if capture is None:
            return {"available": False, "message": "尚未采集真实主板宽度快照", "history": []}
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM breadth_quotes WHERE capture_id=? ORDER BY code", (capture["capture_id"],)
        )]
    quotes = [
        {**row, "risk_warning": bool(row["risk_warning"]), "valid_quote": bool(row["valid_quote"]),
         "traded": bool(row["traded"])} for row in rows
    ]
    meta = {key: value for key, value in dict(capture).items() if key != "quality_json"}
    meta["quality"] = json.loads(capture["quality_json"])
    return {
        "available": True,
        "capture": meta,
        "groups": {
            f"{exchange}_{'ex_st' if remove_st else 'all'}":
                summarize(quotes, exchange=exchange, exclude_risk=remove_st)
            for exchange in ("ALL", "SH", "SZ") for remove_st in (False, True)
        },
        "history": [{
            "capture_id": row["capture_id"], "captured_at_utc": row["captured_at_utc"],
            "trading_date_verified": bool(row["trading_date_verified"]),
        } for row in recent_captures(db_path, limit=25)],
        "historical_trend_ready": False,
        "historical_trend_reason": "尚未核验同一交易日的收盘时点与完整股票池，不能从采集时间直接画交易日历史宽度。",
    }


def _spot_worker(result_queue) -> None:
    try:
        import akshare as ak
        frame = ak.stock_zh_a_spot_em()
        result_queue.put({
            "ok": True,
            "version": str(getattr(ak, "__version__", "unknown")),
            "columns": [] if frame is None else list(frame.columns),
            "records": [] if frame is None else frame.to_dict(orient="records"),
        })
    except Exception as exc:
        result_queue.put({"ok": False, "error": type(exc).__name__ + ": " + str(exc)})


def fetch_spot_em(*, timeout_seconds: int = 90) -> tuple[list[dict], str]:
    """Fetch with a bounded worker; drain the queue before joining the process."""
    if timeout_seconds <= 0:
        raise BreadthError("行情请求超时必须大于零")
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_spot_worker, args=(result_queue,), daemon=True)
    process.start()
    try:
        try:
            payload = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise BreadthError(f"沪深京行情抓取超时或子进程退出（{timeout_seconds}秒）") from exc
    finally:
        process.join(timeout=3)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        result_queue.close()
    if not payload.get("ok"):
        raise BreadthError("行情抓取失败: " + str(payload.get("error", "未知错误")))
    if not set(REQUIRED_COLUMNS).issubset(payload.get("columns") or []):
        raise BreadthError("AKShare沪深京A股快照关键字段发生变化，拒绝保存")
    records = payload.get("records") or []
    if not records:
        raise BreadthError("AKShare沪深京A股快照为空")
    return records, str(payload.get("version") or "unknown")


def validate_against_previous(capture: dict, db_path: str | Path = DEFAULT_DB) -> None:
    """Reject a suspiciously shortened snapshot from the same data provider."""
    if not Path(db_path).exists():
        return
    previous = recent_captures(db_path, limit=1)
    if not previous or previous[0]["source_interface"] != capture["source_interface"]:
        return
    last_quality = json.loads(previous[0]["quality_json"])
    previous_count = int(last_quality.get("identified_mainboard") or 0)
    new_count = int(capture["quality"].get("identified_mainboard") or 0)
    if previous_count >= 1000 and new_count < previous_count * .8:
        raise BreadthError(
            f"本次主板候选仅 {new_count} 条，上次同源为 {previous_count} 条，"
            "可能行情分页截断或接口限流；已保留旧快照"
        )


def update_from_akshare(db_path: str | Path = DEFAULT_DB) -> dict:
    records, version = fetch_spot_em()
    capture = create_capture(
        records, source_name="东方财富 / AKShare", source_interface="ak.stock_zh_a_spot_em",
        version=version
    )
    validate_against_previous(capture, db_path)
    persist_capture(capture, db_path)
    return {"capture_id": capture["capture_id"], "captured_at_utc": capture["captured_at_utc"],
            "quality": capture["quality"], "summary": summarize(capture["quotes"]),
            "note": capture["quote_date_note"]}
