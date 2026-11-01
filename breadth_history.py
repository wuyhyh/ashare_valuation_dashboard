"""Read-only, evidence-gated breadth history. No backfill and no implicit data verification.

A current AKShare spot snapshot is NOT a verified close. Nothing written by
breadth.update_from_akshare() qualifies for this API without a separate,
documented daily-close cross-check implementation (v0.11 Phase B).
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time
import json
from pathlib import Path
import sqlite3

from breadth import CHINA_TZ, SCOPE_BOARDS, SH_SZ_BOARDS, summarize

MIN_CHECKED_SAMPLES = 20
MAX_RELATIVE_DIFF = 0.0005  # 0.05% maximum relative close disagreement
HISTORY_CALCULATION_VERSION = "v011-breadth-v1"
EVIDENCE_METHOD = "independent_stock_daily_close_crosscheck"
MAX_POINTS = 180
LINKAGE_MAX_POINTS = 420
MOVING_AVERAGE_WINDOWS = (5, 20)


def active_daily_sessions(db_path: str | Path, index_code: str = "000300") -> list[str]:
    """Read the active daily index-date sequence as the trading-session calendar."""
    path = Path(db_path)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"series", "active_series", "observations"}.issubset(tables):
            return []
        series = connection.execute(
            "SELECT s.series_id,s.frequency FROM active_series a "
            "JOIN series s ON s.series_id=a.series_id "
            "WHERE a.index_code=? AND a.metric='index_close'",
            (index_code,),
        ).fetchone()
        if series is None or series["frequency"] != "daily":
            return []
        return [row[0] for row in connection.execute(
            "SELECT date FROM observations WHERE series_id=? ORDER BY date",
            (series["series_id"],),
        )]


def session_continuity(dates: list[str], sessions: list[str]) -> dict:
    """Audit whether observations occupy consecutive trusted trading sessions."""
    result = {
        "consecutive": False, "observation_count": len(dates),
        "session_span": None, "missing_session_count": None,
        "calendar_day_span": None,
    }
    if not dates or len(set(dates)) != len(dates) or not sessions:
        return result
    positions = {day: index for index, day in enumerate(sessions)}
    if any(day not in positions for day in dates):
        return result
    indexes = [positions[day] for day in dates]
    if indexes != sorted(indexes):
        return result
    session_span = indexes[-1] - indexes[0] + 1
    result.update({
        "consecutive": session_span == len(dates),
        "session_span": session_span,
        "missing_session_count": session_span - len(dates),
        "calendar_day_span": (
            date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])
        ).days + 1,
    })
    return result


def _eligible(capture: dict, scope: str) -> bool:
    """A verified flag alone must never promote an intraday/undated quote."""
    if not capture["trading_date_verified"] or not capture["trading_date"]:
        return False
    scope_tag = capture["coverage_scope"]
    permitted = (
        ("SH_MAIN", "SZ_MAIN", "MAIN_ALL")
        if scope_tag == "mainboard_only" else
        ("SH_SZ_ALL", "MAIN_ALL", "SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR")
        + (("BSE", "ALL_A") if scope_tag == "sh_sz_all_a_with_bse" else ())
    )
    if scope not in permitted:
        return False
    try:
        trading_day = date.fromisoformat(capture["trading_date"])
        captured = datetime.fromisoformat(capture["captured_at_utc"].replace("Z", "+00:00"))
        if captured.tzinfo is None or trading_day.weekday() >= 5:
            return False
        local = captured.astimezone(CHINA_TZ)
        # Same trading session, after market close. Next-day/weekend reads
        # cannot be passed off as a historical session close.
        if local.date() != trading_day or local.time() < time(15, 30):
            return False
        quality = json.loads(capture["quality_json"])
        evidence = quality.get("close_verification") or {}
        if evidence.get("method") != EVIDENCE_METHOD or evidence.get("status") != "passed":
            return False
        if evidence.get("calculation_version") != HISTORY_CALCULATION_VERSION:
            return False
        if evidence.get("reference_trading_date") != trading_day.isoformat():
            return False
        if not str(evidence.get("reference_source") or "").strip():
            return False
        checked = int(evidence.get("checked_samples", 0))
        matched = int(evidence.get("matched_samples", 0))
        diff = float(evidence.get("max_relative_diff", 999))
        if checked < MIN_CHECKED_SAMPLES or matched < MIN_CHECKED_SAMPLES:
            return False
        if matched > checked or matched / checked < .95:
            return False
        if not 0 <= diff <= MAX_RELATIVE_DIFF:
            return False
        covered = set(evidence.get("checked_boards") or [])
        if not set(SCOPE_BOARDS[scope]).issubset(covered):
            return False
        official = (quality.get("official_pool") or {}).get("boards") or {}
        for board in SCOPE_BOARDS[scope]:
            if float((official.get(board) or {}).get("coverage_ratio", 0)) < .95:
                return False
        return True
    except (KeyError, ValueError, TypeError, OverflowError, json.JSONDecodeError):
        return False


def _validate_query(scope: str, exclude_st: bool) -> None:
    if scope not in SCOPE_BOARDS:
        raise ValueError("未知市场统计范围")
    if type(exclude_st) is not bool:
        raise ValueError("exclude_st 必须是布尔值")


def _load_verified_points(
    db_path: str | Path, *, scope: str, exclude_st: bool, limit: int,
) -> tuple[list[dict], tuple[str, str] | None]:
    """Load one current evidence cohort without changing SQLite."""
    path = Path(db_path)
    if not path.is_file():
        return [], None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(breadth_captures)")}
        if not {"coverage_scope", "trading_date_verified", "trading_date", "quality_json"}.issubset(columns):
            return [], None
        captures = conn.execute(
            "SELECT * FROM breadth_captures WHERE trading_date_verified=1 "
            "AND trading_date IS NOT NULL ORDER BY captured_at_utc DESC LIMIT 5000"
        ).fetchall()
        chosen = {}
        cohort = None
        for record in captures:
            item = dict(record)
            if not _eligible(item, scope):
                continue
            key = (item["source_interface"], item["coverage_scope"])
            if cohort is None:
                cohort = key
            if key != cohort:
                continue
            day = item["trading_date"]
            if day not in chosen:
                chosen[day] = item
                if len(chosen) == limit:
                    break
        if cohort is None:
            return [], None
        points = []
        for day in sorted(chosen):
            item = chosen[day]
            quotes = [
                {**dict(row), "risk_warning": bool(row["risk_warning"]),
                 "valid_quote": bool(row["valid_quote"]), "traded": bool(row["traded"])}
                for row in conn.execute(
                    "SELECT * FROM breadth_quotes WHERE capture_id=?",
                    (item["capture_id"],),
                )
            ]
            metrics = summarize(quotes, scope=scope, exclude_risk=exclude_st)
            if not metrics["traded_count"]:
                continue
            points.append({
                "trading_date": day, "advance_ratio": metrics["advance_ratio"],
                "advances": metrics["advances"], "declines": metrics["declines"],
                "unchanged": metrics["unchanged"], "traded_count": metrics["traded_count"],
                "identified_count": metrics["identified_count"],
                "median_change_pct": metrics["median_change_pct"],
                "turnover_weighted_change_pct": metrics["turnover_weighted_change_pct"],
            })
        return points, cohort


def breadth_linkage_history(
    db_path: str | Path, *, scope: str = "SH_SZ_ALL",
    exclude_st: bool = False, limit: int = LINKAGE_MAX_POINTS,
) -> dict:
    """Return a compact long horizon for exact-date linkage, not charting."""
    _validate_query(scope, exclude_st)
    if type(limit) is not int or not 2 <= limit <= LINKAGE_MAX_POINTS:
        raise ValueError("联动历史窗口必须为2到420个已核验交易日")
    points, cohort = _load_verified_points(
        db_path, scope=scope, exclude_st=exclude_st, limit=limit,
    )
    return {
        "available": len(points) >= 2,
        "scope": scope, "exclude_st": exclude_st,
        "observations": points, "point_count": len(points),
        "query_limit": limit,
        "source_interface": None if cohort is None else cohort[0],
        "coverage_scope": None if cohort is None else cohort[1],
        "calculation_version": HISTORY_CALCULATION_VERSION,
        "message": (
            "长期联动历史只使用同一来源、覆盖范围和统计版本的已核验交易日"
            if len(points) >= 2 else
            "长期联动历史不足；不会用图表窗口、插值或其他来源补齐"
        ),
    }


def breadth_trend(db_path: str | Path, *, scope: str = "SH_SZ_ALL",
                  exclude_st: bool = False, limit: int = 90,
                  calendar_db_path: str | Path | None = None) -> dict:
    """Read a chart-limited comparable cohort, one capture per trade day."""
    _validate_query(scope, exclude_st)
    if type(limit) is not int or not 2 <= limit <= MAX_POINTS:
        raise ValueError("历史窗口必须为2到180个已核验交易日")
    result = {
        "available": False, "scope": scope, "exclude_st": exclude_st,
        "observations": [], "point_count": 0, "required_points": 2,
        "source_interface": None, "coverage_scope": None,
        "calculation_version": HISTORY_CALCULATION_VERSION,
        "moving_averages": {
            "windows": list(MOVING_AVERAGE_WINDOWS),
            "definition": "consecutive_verified_trading_session_simple_mean",
            "calendar_index": "000300 index_close active daily",
            "calendar_available": False, "ma5_ready": False, "ma20_ready": False,
        },
        "message": "尚无至少两个具有独立收盘价核验证据的可比交易日；当前行情快照不能绘制历史趋势",
    }
    points, cohort = _load_verified_points(
        db_path, scope=scope, exclude_st=exclude_st, limit=limit,
    )
    sessions = active_daily_sessions(calendar_db_path) if calendar_db_path else []
    for index, point in enumerate(points):
        for window in MOVING_AVERAGE_WINDOWS:
            field = f"advance_ratio_ma{window}"
            audit_field = f"advance_ratio_ma{window}_continuity"
            if index + 1 < window:
                point[field] = None
                point[audit_field] = None
                continue
            sample = points[index + 1 - window:index + 1]
            audit = session_continuity(
                [item["trading_date"] for item in sample], sessions,
            )
            point[audit_field] = audit
            point[field] = (
                sum(item["advance_ratio"] for item in sample) / window
                if audit["consecutive"] else None
            )
    result.update({
        "observations": points,
        "point_count": len(points),
        "available": len(points) >= 2,
        "source_interface": None if cohort is None else cohort[0],
        "coverage_scope": None if cohort is None else cohort[1],
    })
    result["moving_averages"].update({
        "calendar_available": bool(sessions),
        "ma5_ready": any(point["advance_ratio_ma5"] is not None for point in points),
        "ma20_ready": any(point["advance_ratio_ma20"] is not None for point in points),
    })
    if len(points) >= 2:
        result["message"] = (
            "仅展示具有独立真实收盘核验证据的同源同口径交易日；"
            "日期缺口不填充，不代表宽度与指数涨跌有确定因果关系"
        )
    return result
