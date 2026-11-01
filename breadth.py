"""Point-in-time A-share market breadth snapshots by exchange board.

An intraday quote snapshot does NOT prove its exchange trade date. Snapshot
capture times are distinct from exchange trading dates; no historical backfill
is manufactured. This module never fetches data unless explicitly requested.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import json
import math
import multiprocessing
from pathlib import Path
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
CHINEXT_PREFIXES = ("300", "301")
STAR_PREFIXES = ("688",)
REQUIRED_COLUMNS = ("代码", "名称", "最新价", "涨跌幅", "成交额")
BOARD_INFO = {
    "SH_MAIN": {"label": "上海主板", "exchange": "SH"},
    "SZ_MAIN": {"label": "深圳主板", "exchange": "SZ"},
    "CHINEXT": {"label": "创业板", "exchange": "SZ"},
    "STAR": {"label": "科创板", "exchange": "SH"},
    "BSE": {"label": "北交所", "exchange": "BJ"},
}
SH_SZ_BOARDS = ("SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR")
ALL_BOARDS = (*SH_SZ_BOARDS, "BSE")
SCOPE_BOARDS = {
    "SH_MAIN": ("SH_MAIN",),
    "SZ_MAIN": ("SZ_MAIN",),
    "CHINEXT": ("CHINEXT",),
    "STAR": ("STAR",),
    "MAIN_ALL": ("SH_MAIN", "SZ_MAIN"),
    "SH_SZ_ALL": SH_SZ_BOARDS,
    "BSE": ("BSE",),
    "ALL_A": ALL_BOARDS,
}
LEGACY_SCOPE_ALIASES = {"SH": "SH_MAIN", "SZ": "SZ_MAIN", "ALL": "MAIN_ALL"}
BREADTH_PROVIDERS = {
    "eastmoney": {
        "source_name": "东方财富 / AKShare",
        "source_interface": "ak.stock_zh_a_spot_em",
        "required_columns": REQUIRED_COLUMNS,
    },
    "tencent": {
        "source_name": "腾讯证券 / AKShare",
        "source_interface": "ak.stock_zh_a_spot_tx",
        "required_columns": ("code", "name", "zxj", "zdf", "turnover"),
    },
    "sina": {
        "source_name": "新浪财经 / AKShare",
        "source_interface": "ak.stock_zh_a_spot",
        "required_columns": REQUIRED_COLUMNS,
    },
}
AUTO_PROVIDER_ORDER = ("eastmoney", "tencent", "sina")


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


def fallback_board(code: str) -> str | None:
    """Conservative fallback for fixtures; live identity comes from official lists."""
    exchange = mainboard_exchange(code)
    if exchange == "SH":
        return "SH_MAIN"
    if exchange == "SZ":
        return "SZ_MAIN"
    if re.fullmatch(r"\d{6}", str(code)) and str(code).startswith(CHINEXT_PREFIXES):
        return "CHINEXT"
    if re.fullmatch(r"\d{6}", str(code)) and str(code).startswith(STAR_PREFIXES):
        return "STAR"
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


def _canonical_quote_code(value) -> str:
    code = str(value or "").strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        code = code[2:]
    return code


def adapt_spot_records(records: list[dict], provider: str) -> list[dict]:
    """Convert a verified provider schema into the canonical breadth fields."""
    if provider not in BREADTH_PROVIDERS:
        raise BreadthError("不支持的市场宽度数据源: " + str(provider))
    adapted = []
    for raw in records:
        if provider == "tencent":
            turnover = finite_float(raw.get("turnover"))
            adapted.append({
                "代码": _canonical_quote_code(raw.get("code")),
                "名称": raw.get("name"),
                "最新价": raw.get("zxj"),
                "涨跌幅": raw.get("zdf"),
                # Tencent returns turnover in 10,000 CNY. Store CNY so all
                # providers share the same explicit unit.
                "成交额": turnover * 10_000 if turnover is not None else None,
            })
        else:
            adapted.append({
                "代码": _canonical_quote_code(raw.get("代码")),
                "名称": raw.get("名称"),
                "最新价": raw.get("最新价"),
                "涨跌幅": raw.get("涨跌幅"),
                "成交额": raw.get("成交额"),
            })
    return adapted


def normalize_quotes(records: list[dict], official_pool: dict[str, dict] | None = None) -> tuple[list[dict], dict]:
    if not isinstance(records, list) or not records:
        raise BreadthError("上游快照为空，不能生成市场宽度")
    if not REQUIRED_COLUMNS or not set(REQUIRED_COLUMNS).issubset(set(records[0])):
        raise BreadthError("上游字段缺失（需要代码、名称、最新价、涨跌幅、成交额）")
    quotes = []
    seen = set()
    excluded = {"not_in_official_pool": 0, "bad_code": 0, "duplicate_code": 0}
    unexpected_quote_codes = []
    for raw in records:
        code = str(raw.get("代码", "")).strip()
        if not re.fullmatch(r"\d{6}", code):
            excluded["bad_code"] += 1
            continue
        identity = official_pool.get(code) if official_pool is not None else None
        if official_pool is not None and identity is None:
            excluded["not_in_official_pool"] += 1
            unexpected_quote_codes.append(code)
            continue
        board = identity.get("board") if identity else fallback_board(code)
        if board not in BOARD_INFO:
            excluded["not_in_official_pool"] += 1
            continue
        exchange = identity.get("exchange") if identity else BOARD_INFO[board]["exchange"]
        security_type = identity.get("security_type", "A_SHARE") if identity else "A_SHARE"
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
            "code": code, "name": name, "exchange": exchange, "board": board,
            "security_type": security_type,
            "risk_warning": risk_warning(name),
            "price": price, "change_pct": change, "turnover": turnover,
            "valid_quote": quoted, "traded": traded,
        })
    extreme = [row["code"] for row in quotes if row["change_pct"] is not None and abs(row["change_pct"]) > 30]
    special_count = sum(row["security_type"] != "A_SHARE" for row in quotes)
    return quotes, {
        "raw_count": len(records),
        "identified_scope_count": sum(row["security_type"] == "A_SHARE" for row in quotes),
        "identified_mainboard": sum(
            row["security_type"] == "A_SHARE" and row["board"] in ("SH_MAIN", "SZ_MAIN")
            for row in quotes
        ),
        "identified_sh_sz_a": sum(
            row["security_type"] == "A_SHARE" and row["board"] in SH_SZ_BOARDS for row in quotes
        ),
        "identified_bse": sum(
            row["security_type"] == "A_SHARE" and row["board"] == "BSE" for row in quotes
        ),
        "excluded_special_security": special_count,
        "unexpected_quote_count": len(unexpected_quote_codes),
        "unexpected_quote_sample": unexpected_quote_codes[:20],
        "exclusion_reasons": {
            "outside_official_pool": len(unexpected_quote_codes),
            "special_security_type": special_count,
            "invalid_code": excluded["bad_code"],
        },
        **excluded,
        "extreme_change_count": len(extreme), "extreme_change_sample": extreme[:20],
    }


def audit_official_pool(quotes: list[dict], official_pool: dict[str, dict]) -> dict:
    if not official_pool:
        raise BreadthError("交易所官方股票池为空，拒绝将快照标记为完整")
    observed = {row["code"] for row in quotes if row["security_type"] == "A_SHARE"}
    expected = {code for code, item in official_pool.items() if item.get("security_type", "A_SHARE") == "A_SHARE"}
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    def identity_name(value):
        name = re.sub(r"\s+", "", str(value)).upper()
        return re.sub(r"-(?:UW|WD|U|W)$", "", name)

    quote_names = {row["code"]: identity_name(row["name"]) for row in quotes}
    name_mismatches = sorted(
        code for code in expected & observed
        if quote_names.get(code) != identity_name(official_pool[code]["name"])
    )
    boards = {}
    for board in ALL_BOARDS:
        expected_board = {
            code for code, item in official_pool.items()
            if item.get("board") == board and item.get("security_type", "A_SHARE") == "A_SHARE"
        }
        observed_board = {
            row["code"] for row in quotes
            if row["board"] == board and row["security_type"] == "A_SHARE"
        }
        if not expected_board:
            continue
        boards[board] = {
            "label": BOARD_INFO[board]["label"],
            "official_count": len(expected_board),
            "observed_count": len(observed_board),
            "matched_count": len(expected_board & observed_board),
            "coverage_ratio": len(expected_board & observed_board) / len(expected_board),
            "missing_count": len(expected_board - observed_board),
            "unexpected_count": len(observed_board - expected_board),
        }
    special = sorted(code for code, item in official_pool.items() if item.get("security_type") != "A_SHARE")
    return {
        "source": (
            "ak.stock_info_sh_name_code(主板A股/科创板) + "
            "ak.stock_info_sz_name_code(A股列表) + ak.stock_info_bj_name_code"
        ),
        "official_count": len(expected),
        "matched_count": len(expected & observed),
        "coverage_ratio": len(expected & observed) / len(expected),
        "missing_count": len(missing),
        "unexpected_count": len(unexpected),
        "missing_sample": missing[:20],
        "unexpected_sample": unexpected[:20],
        "name_mismatch_count": len(name_mismatches),
        "name_mismatch_sample": name_mismatches[:20],
        "boards": boards,
        "special_security_count": len(special),
        "special_security_sample": special[:20],
    }


def summarize(quotes: list[dict], *, scope: str = "MAIN_ALL", exchange: str | None = None,
              exclude_risk: bool = False) -> dict:
    if exchange is not None:
        scope = LEGACY_SCOPE_ALIASES.get(exchange, exchange)
    scope = LEGACY_SCOPE_ALIASES.get(scope, scope)
    if scope not in SCOPE_BOARDS:
        raise BreadthError(f"不支持的市场范围: {scope}")
    boards = SCOPE_BOARDS[scope]
    pool = [
        row for row in quotes
        if row.get("board") in boards and row.get("security_type", "A_SHARE") == "A_SHARE"
    ]
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
        "scope": scope,
        "boards": list(boards),
        "exclude_risk": exclude_risk,
        "identified_mainboard": len(pool),
        "identified_count": len(pool),
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
        "interpretation": "仅统计当前同源同时点行情快照中、经交易所名录确认且有效成交额大于0的样本。",
    }


def create_capture(records: list[dict], *, source_interface: str, source_name: str,
                   version: str = "unknown", captured_at: datetime | None = None,
                   min_identified: int = 4500, official_pool: dict[str, dict] | None = None,
                   min_official_coverage: float = .95, extra_quality: dict | None = None) -> dict:
    if not source_interface.strip() or not source_name.strip():
        raise BreadthError("必须提供数据来源与接口名称")
    if captured_at is None:
        captured_at = datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        raise BreadthError("采集时间必须包含时区")
    captured_at = captured_at.astimezone(timezone.utc)
    quotes, checks = normalize_quotes(records, official_pool=official_pool)
    identified = checks["identified_sh_sz_a"] if official_pool is not None else len(quotes)
    if identified < min_identified:
        raise BreadthError(f"沪深A股候选仅 {identified} 条（至少需要 {min_identified}）；可能上游分页缺失，拒绝保存")
    valid = sum(item["traded"] for item in quotes if item["security_type"] == "A_SHARE")
    a_share_count = sum(item["security_type"] == "A_SHARE" for item in quotes)
    if valid < max(1, int(a_share_count * .70)):
        raise BreadthError(f"有效成交股票仅 {valid}/{a_share_count}，可能行情字段异常；拒绝保存")
    if official_pool is not None:
        official_audit = audit_official_pool(quotes, official_pool)
        failed = {
            board: item["coverage_ratio"] for board, item in official_audit["boards"].items()
            if board in SH_SZ_BOARDS and item["coverage_ratio"] < min_official_coverage
        }
        if failed:
            detail = " / ".join(f"{board} {ratio:.1%}" for board, ratio in failed.items())
            raise BreadthError(f"行情对官方板块名录覆盖不足（{detail}），拒绝保存")
        checks["official_pool"] = official_audit
        bse = official_audit["boards"].get("BSE")
        checks["bse_coverage_ready"] = bool(
            bse and bse["coverage_ratio"] >= min_official_coverage
        )
    if extra_quality:
        checks.update(extra_quality)
    return {
        "capture_id": uuid.uuid4().hex,
        "captured_at_utc": captured_at.isoformat(),
        "capture_local_date": captured_at.astimezone(CHINA_TZ).date().isoformat(),
        "trading_date": None,
        "trading_date_verified": False,
        "source_name": source_name, "source_interface": source_interface,
        "source_version": version,
        "coverage_scope": (
            "mainboard_only" if official_pool is None else
            "sh_sz_all_a_with_bse" if checks.get("bse_coverage_ready") else
            "sh_sz_all_a"
        ),
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
        quality_json TEXT NOT NULL,
        coverage_scope TEXT NOT NULL DEFAULT 'mainboard_only'
    );
    CREATE TABLE IF NOT EXISTS breadth_quotes (
        capture_id TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        exchange TEXT NOT NULL,
        board TEXT,
        security_type TEXT NOT NULL DEFAULT 'A_SHARE',
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
    capture_columns = {row[1] for row in connection.execute("PRAGMA table_info(breadth_captures)")}
    if "coverage_scope" not in capture_columns:
        connection.execute(
            "ALTER TABLE breadth_captures ADD COLUMN coverage_scope TEXT NOT NULL DEFAULT 'mainboard_only'"
        )
    quote_columns = {row[1] for row in connection.execute("PRAGMA table_info(breadth_quotes)")}
    if "board" not in quote_columns:
        connection.execute("ALTER TABLE breadth_quotes ADD COLUMN board TEXT")
    if "security_type" not in quote_columns:
        connection.execute(
            "ALTER TABLE breadth_quotes ADD COLUMN security_type TEXT NOT NULL DEFAULT 'A_SHARE'"
        )
    connection.execute(
        "UPDATE breadth_quotes SET board=CASE exchange "
        "WHEN 'SH' THEN 'SH_MAIN' WHEN 'SZ' THEN 'SZ_MAIN' END WHERE board IS NULL"
    )
    return connection


def persist_capture(capture: dict, db_path: str | Path = DEFAULT_DB) -> str:
    with closing(open_db(db_path)) as db, db:
        db.execute("""
            INSERT INTO breadth_captures (
                capture_id,captured_at_utc,capture_local_date,trading_date,
                trading_date_verified,source_name,source_interface,source_version,
                quote_date_note,quality_json,coverage_scope
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            capture["capture_id"], capture["captured_at_utc"], capture["capture_local_date"],
            capture["trading_date"], int(capture["trading_date_verified"]),
            capture["source_name"], capture["source_interface"], capture["source_version"],
            capture["quote_date_note"], json.dumps(capture["quality"], ensure_ascii=False),
            capture["coverage_scope"],
        ))
        db.executemany("""
            INSERT INTO breadth_quotes (
                capture_id,code,name,exchange,board,security_type,risk_warning,
                price,change_pct,turnover,valid_quote,traded
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            (capture["capture_id"], q["code"], q["name"], q["exchange"], q["board"],
             q["security_type"], int(q["risk_warning"]), q["price"], q["change_pct"],
             q["turnover"], int(q["valid_quote"]), int(q["traded"]))
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
        return {"available": False, "message": "尚未采集真实市场宽度快照", "history": []}
    with closing(open_db(db_path)) as db, db:
        capture = db.execute(
            "SELECT * FROM breadth_captures ORDER BY captured_at_utc DESC, capture_id DESC LIMIT 1"
        ).fetchone()
        if capture is None:
            return {"available": False, "message": "尚未采集真实市场宽度快照", "history": []}
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM breadth_quotes WHERE capture_id=? ORDER BY code", (capture["capture_id"],)
        )]
    quotes = [
        {**row, "risk_warning": bool(row["risk_warning"]), "valid_quote": bool(row["valid_quote"]),
         "traded": bool(row["traded"])} for row in rows
    ]
    meta = {key: value for key, value in dict(capture).items() if key != "quality_json"}
    meta["quality"] = json.loads(capture["quality_json"])
    is_legacy = meta["coverage_scope"] == "mainboard_only"
    if is_legacy:
        scopes = ("SH_MAIN", "SZ_MAIN", "MAIN_ALL")
    else:
        scopes = ("SH_SZ_ALL", "MAIN_ALL", "SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR")
        if meta["coverage_scope"] == "sh_sz_all_a_with_bse":
            scopes = (*scopes, "BSE", "ALL_A")
    groups = {
        f"{scope}_{'ex_st' if remove_st else 'all'}":
            summarize(quotes, scope=scope, exclude_risk=remove_st)
        for scope in scopes for remove_st in (False, True)
    }
    for legacy, scope in LEGACY_SCOPE_ALIASES.items():
        if scope in scopes:
            for suffix in ("all", "ex_st"):
                groups[f"{legacy}_{suffix}"] = groups[f"{scope}_{suffix}"]
    return {
        "available": True,
        "capture": meta,
        "groups": groups,
        "available_scopes": list(scopes),
        "default_scope": "MAIN_ALL" if is_legacy else "SH_SZ_ALL",
        "market_universe": {
            "default_scope": "MAIN_ALL" if is_legacy else "SH_SZ_ALL",
            "definition": (
                "v0.9 沪深主板快照" if is_legacy else
                "沪深A股：上海主板+深圳主板+创业板+科创板，不含689 CDR"
            ),
            "bse_separate": "BSE" in scopes,
        },
        "investment_universe": {
            "applied_to_market_breadth": False,
            "note": "个人交易权限和自选范围不裁剪市场宽度分母。",
        },
        "scope_warning": (
            "该快照来自 v0.9，仅覆盖沪深主板；不能解释为沪深全A股。"
            if is_legacy else None
        ),
        "history": [{
            "capture_id": row["capture_id"], "captured_at_utc": row["captured_at_utc"],
            "trading_date_verified": bool(row["trading_date_verified"]),
            "coverage_scope": row["coverage_scope"],
        } for row in recent_captures(db_path, limit=25)],
        "historical_trend_ready": False,
        "historical_trend_reason": "尚未核验同一交易日的收盘时点与完整股票池，不能从采集时间直接画交易日历史宽度。",
    }


def _spot_worker(provider: str, result_queue) -> None:
    try:
        import akshare as ak
        if provider == "eastmoney":
            frame = ak.stock_zh_a_spot_em()
        elif provider == "tencent":
            frame = ak.stock_zh_a_spot_tx()
        elif provider == "sina":
            frame = ak.stock_zh_a_spot()
        else:
            raise BreadthError("不支持的市场宽度数据源: " + str(provider))
        result_queue.put({
            "ok": True,
            "version": str(getattr(ak, "__version__", "unknown")),
            "columns": [] if frame is None else list(frame.columns),
            "records": [] if frame is None else frame.to_dict(orient="records"),
        })
    except Exception as exc:
        result_queue.put({"ok": False, "error": type(exc).__name__ + ": " + str(exc)})


def validate_against_previous(capture: dict, db_path: str | Path = DEFAULT_DB) -> None:
    """Reject a suspiciously shortened snapshot from the same data provider."""
    if not Path(db_path).exists():
        return
    previous = recent_captures(db_path, limit=1)
    if (
        not previous
        or previous[0]["source_interface"] != capture["source_interface"]
        or previous[0].get("coverage_scope") != capture["coverage_scope"]
    ):
        return
    last_quality = json.loads(previous[0]["quality_json"])
    previous_count = int(last_quality.get("identified_scope_count") or last_quality.get("identified_mainboard") or 0)
    new_count = int(capture["quality"].get("identified_scope_count") or 0)
    if previous_count >= 1000 and new_count < previous_count * .8:
        raise BreadthError(
            f"本次同口径候选仅 {new_count} 条，上次同源为 {previous_count} 条，"
            "可能行情分页截断或接口限流；已保留旧快照"
        )


def _official_pool_worker(result_queue) -> None:
    try:
        import akshare as ak
        sh_main = ak.stock_info_sh_name_code(symbol="主板A股")
        sh_star = ak.stock_info_sh_name_code(symbol="科创板")
        sz = ak.stock_info_sz_name_code(symbol="A股列表")
        bse = None
        bse_error = None
        try:
            bse = ak.stock_info_bj_name_code()
        except Exception as exc:
            bse_error = type(exc).__name__ + ": " + str(exc)
        result_queue.put({
            "ok": True,
            "sh_main_columns": [] if sh_main is None else list(sh_main.columns),
            "sh_star_columns": [] if sh_star is None else list(sh_star.columns),
            "sz_columns": [] if sz is None else list(sz.columns),
            "bse_columns": [] if bse is None else list(bse.columns),
            "sh_main": [] if sh_main is None else sh_main.to_dict(orient="records"),
            "sh_star": [] if sh_star is None else sh_star.to_dict(orient="records"),
            "sz": [] if sz is None else sz.to_dict(orient="records"),
            "bse": [] if bse is None else bse.to_dict(orient="records"),
            "bse_error": bse_error,
        })
    except Exception as exc:
        result_queue.put({"ok": False, "error": type(exc).__name__ + ": " + str(exc)})


def _run_worker(target, args: tuple, timeout_seconds: int) -> dict:
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue(maxsize=1)
    process = ctx.Process(target=target, args=(*args, results), daemon=True)
    process.start()
    try:
        try:
            payload = results.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise BreadthError(f"AKShare请求超时（{timeout_seconds}秒）") from exc
    finally:
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        results.close()
    return payload


def fetch_spot(provider: str = "auto", timeout_seconds: int = 150, fetcher=None) -> dict:
    providers = AUTO_PROVIDER_ORDER if provider == "auto" else (provider,)
    errors = []
    for candidate in providers:
        if candidate not in BREADTH_PROVIDERS:
            raise BreadthError("不支持的市场宽度数据源: " + str(candidate))
        payload = fetcher(candidate) if fetcher else _run_worker(_spot_worker, (candidate,), timeout_seconds)
        if not isinstance(payload, dict) or not payload.get("ok"):
            errors.append({"provider": candidate, "error": str((payload or {}).get("error", "未知错误"))})
            continue
        raw = payload.get("records") or []
        if not raw:
            errors.append({"provider": candidate, "error": "上游快照为空"})
            continue
        native_columns = set(payload.get("columns") or [])
        required = set(BREADTH_PROVIDERS[candidate]["required_columns"])
        if not required.issubset(native_columns):
            errors.append({
                "provider": candidate,
                "error": "上游快照字段发生变化，缺失 " + ", ".join(sorted(required - native_columns)),
            })
            continue
        records = adapt_spot_records(raw, candidate)
        return {
            "provider": candidate,
            "records": records,
            "version": str(payload.get("version") or "unknown"),
            "native_columns": list(payload.get("columns") or []),
            "attempts": [*errors, {"provider": candidate, "status": "success"}],
            **BREADTH_PROVIDERS[candidate],
        }
    raise BreadthError("市场宽度行情源全部失败: " + json.dumps(errors, ensure_ascii=False))


def fetch_spot_em(*, timeout_seconds: int = 90) -> tuple[list[dict], str]:
    """Backward-compatible bounded Eastmoney fetch used by existing callers."""
    result = fetch_spot("eastmoney", timeout_seconds=timeout_seconds)
    return result["records"], result["version"]


def fetch_official_pool(timeout_seconds: int = 90, fetcher=None) -> dict:
    payload = fetcher() if fetcher else _run_worker(_official_pool_worker, (), timeout_seconds)
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise BreadthError("交易所官方股票名录获取失败: " + str((payload or {}).get("error", "未知错误")))
    if not {"证券代码", "证券简称"}.issubset(payload.get("sh_main_columns") or []):
        raise BreadthError("上交所主板名录字段发生变化")
    if not {"证券代码", "证券简称"}.issubset(payload.get("sh_star_columns") or []):
        raise BreadthError("上交所科创板名录字段发生变化")
    if not {"板块", "A股代码", "A股简称"}.issubset(payload.get("sz_columns") or []):
        raise BreadthError("深交所A股名录字段发生变化")
    if payload.get("bse") and not {"证券代码", "证券简称"}.issubset(payload.get("bse_columns") or []):
        raise BreadthError("北交所名录字段发生变化")
    pool = {}

    def add(code, name, board, security_type="A_SHARE"):
        code = _canonical_quote_code(code)
        if not re.fullmatch(r"\d{6}", code):
            raise BreadthError(f"官方股票池包含无效代码: {code}")
        if code in pool:
            raise BreadthError(f"官方股票池重复代码: {code}")
        pool[code] = {
            "exchange": BOARD_INFO[board]["exchange"], "board": board,
            "security_type": security_type, "name": str(name or "").strip(),
        }

    for item in payload.get("sh_main") or []:
        code = _canonical_quote_code(item.get("证券代码"))
        if mainboard_exchange(code) == "SH":
            add(code, item.get("证券简称"), "SH_MAIN")
    for item in payload.get("sh_star") or []:
        code = _canonical_quote_code(item.get("证券代码"))
        if code.startswith("688"):
            add(code, item.get("证券简称"), "STAR")
        elif code.startswith("689"):
            add(code, item.get("证券简称"), "STAR", "CDR")
        else:
            raise BreadthError(f"科创板名录出现未核验代码前缀: {code}")
    for item in payload.get("sz") or []:
        code = _canonical_quote_code(item.get("A股代码"))
        board_label = str(item.get("板块") or "").strip()
        if board_label == "主板" and mainboard_exchange(code) == "SZ":
            add(code, item.get("A股简称"), "SZ_MAIN")
        elif board_label == "创业板":
            add(code, item.get("A股简称"), "CHINEXT")
        else:
            raise BreadthError(f"深交所A股名录出现未核验板块或代码: {board_label} {code}")
    for item in payload.get("bse") or []:
        add(item.get("证券代码"), item.get("证券简称"), "BSE")
    counts = {
        board: sum(
            item["board"] == board and item["security_type"] == "A_SHARE"
            for item in pool.values()
        ) for board in ALL_BOARDS
    }
    if any(counts[board] == 0 for board in SH_SZ_BOARDS):
        raise BreadthError("交易所官方名录缺少沪深主板、创业板或科创板数据")
    return {"members": pool, "board_counts": counts, "bse_error": payload.get("bse_error")}


def update_from_akshare(db_path: str | Path = DEFAULT_DB, *, provider: str = "auto",
                        spot_fetcher=None, pool_fetcher=None) -> dict:
    spot = fetch_spot(provider, fetcher=spot_fetcher)
    official_result = fetch_official_pool(fetcher=pool_fetcher)
    official_pool = official_result["members"]
    capture = create_capture(
        spot["records"], source_name=spot["source_name"], source_interface=spot["source_interface"],
        version=spot["version"], official_pool=official_pool,
        extra_quality={
            "native_columns": spot["native_columns"],
            "provider": spot["provider"],
            "provider_attempts": spot["attempts"],
            "turnover_unit": "CNY",
            "official_board_counts": official_result["board_counts"],
            "bse_official_error": official_result["bse_error"],
        },
    )
    validate_against_previous(capture, db_path)
    persist_capture(capture, db_path)
    return {"capture_id": capture["capture_id"], "captured_at_utc": capture["captured_at_utc"],
            "coverage_scope": capture["coverage_scope"], "quality": capture["quality"],
            "summary": summarize(capture["quotes"], scope="SH_SZ_ALL"),
            "note": capture["quote_date_note"]}
