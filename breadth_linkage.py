"""Evidence-gated breadth linkage using exact real observation dates only."""
from __future__ import annotations

from contextlib import closing
from datetime import date
import math
from pathlib import Path
import sqlite3

from breadth_history import LINKAGE_MAX_POINTS, breadth_linkage_history


LINKED_INDICES = ("000300", "000905", "000852")
VALUATION_METRICS = ("pe_ttm", "pb")
MIN_DIVERGENCE_POINTS = 20
MIN_VALUATION_POINTS = 12
MAX_LINKAGE_GAP_DAYS = 7


def _active_series(db_path: str | Path, index_code: str, metric: str) -> dict | None:
    path = Path(db_path)
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"series", "active_series", "observations"}.issubset(tables):
            return None
        series = connection.execute(
            "SELECT s.* FROM active_series a JOIN series s ON s.series_id=a.series_id "
            "WHERE a.index_code=? AND a.metric=?",
            (index_code, metric),
        ).fetchone()
        if series is None:
            return None
        rows = [dict(row) for row in connection.execute(
            "SELECT date,value FROM observations WHERE series_id=? ORDER BY date",
            (series["series_id"],),
        )]
        return {**dict(series), "rows": rows}


def _exact_pairs(breadth_by_date: dict[str, float], series: dict | None) -> list[tuple[str, float, float]]:
    if series is None:
        return []
    return [
        (row["date"], breadth_by_date[row["date"]], float(row["value"]))
        for row in series["rows"]
        if row["date"] in breadth_by_date and float(row["value"]) > 0
    ]


def _dense(dates: list[str]) -> bool:
    parsed = [date.fromisoformat(value) for value in dates]
    return all((right - left).days <= MAX_LINKAGE_GAP_DAYS for left, right in zip(parsed, parsed[1:]))


def _correlation(left: list[float], right: list[float]) -> float | None:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if not left_scale or not right_scale:
        return None
    return covariance / (left_scale * right_scale)


def _month_number(value: str) -> int:
    year, month = map(int, value[:7].split("-"))
    return year * 12 + month - 1


def _month_label(value: int) -> str:
    year, month = divmod(value, 12)
    return f"{year:04d}-{month + 1:02d}"


def _monthly_audit(pairs: list[tuple[str, float, float]]) -> dict:
    month_counts = {}
    for day, _, _ in pairs:
        month = day[:7]
        month_counts[month] = month_counts.get(month, 0) + 1
    months = sorted(month_counts, key=_month_number)
    if not months:
        return {
            "common_months": 0, "calendar_month_span": 0,
            "missing_months": [], "missing_month_count": 0,
            "max_missing_consecutive_months": 0, "duplicate_months": [],
            "month_coverage_ratio": 0,
        }
    start = _month_number(months[0])
    end = _month_number(months[-1])
    present = {_month_number(month) for month in months}
    missing = [value for value in range(start, end + 1) if value not in present]
    longest = current = 0
    for value in range(start, end + 1):
        if value in present:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    expected = end - start + 1
    duplicates = [month for month in months if month_counts[month] > 1]
    return {
        "common_months": len(months), "calendar_month_span": expected,
        "missing_months": [_month_label(value) for value in missing],
        "missing_month_count": len(missing),
        "max_missing_consecutive_months": longest,
        "duplicate_months": duplicates,
        "month_coverage_ratio": len(months) / expected,
    }


def breadth_linkage(
    breadth_db: str | Path,
    valuation_db: str | Path,
    *,
    scope: str = "SH_SZ_ALL",
    exclude_st: bool = False,
) -> dict:
    """Compare verified breadth with active index/valuation series on exact dates.

    No date is shifted, forward-filled or interpolated. Results remain unavailable
    until their explicit minimum sample and density requirements are satisfied.
    """
    trend = breadth_linkage_history(
        breadth_db, scope=scope, exclude_st=exclude_st, limit=LINKAGE_MAX_POINTS,
    )
    result = {
        "available": False,
        "scope": scope,
        "exclude_st": exclude_st,
        "breadth_points": trend["point_count"],
        "breadth_history_query_limit": LINKAGE_MAX_POINTS,
        "breadth_source_interface": trend.get("source_interface"),
        "breadth_coverage_scope": trend.get("coverage_scope"),
        "matching_rule": "exact_same_date_no_fill_no_interpolation",
        "divergence_minimum": MIN_DIVERGENCE_POINTS,
        "valuation_minimum": MIN_VALUATION_POINTS,
        "indices": {},
        "message": "可信宽度历史样本不足，暂不计算指数背离或估值关系",
    }
    if not trend["available"]:
        return result
    breadth_by_date = {
        point["trading_date"]: float(point["advance_ratio"])
        for point in trend["observations"]
    }
    any_ready = False
    for index_code in LINKED_INDICES:
        index_series = _active_series(valuation_db, index_code, "index_close")
        index_pairs = _exact_pairs(breadth_by_date, index_series)
        divergence = {
            "available": False,
            "common_dates": len(index_pairs),
            "required_common_dates": MIN_DIVERGENCE_POINTS,
            "message": "至少需要20个稠密、严格同日的真实宽度与指数点位样本",
            "calculation": "上涨比例百分点变化 − 指数百分比收益；仅为不同指标的探索性差值",
        }
        window = index_pairs[-MIN_DIVERGENCE_POINTS:]
        if index_series is not None and index_series.get("frequency") != "daily":
            divergence["message"] = "指数点位不是已识别的真实日频序列，拒绝计算背离"
        elif len(window) == MIN_DIVERGENCE_POINTS and _dense([item[0] for item in window]):
            breadth_change = (window[-1][1] - window[0][1]) * 100
            index_return = (window[-1][2] / window[0][2] - 1) * 100
            divergence = {
                "available": True,
                "common_dates": len(index_pairs),
                "required_common_dates": MIN_DIVERGENCE_POINTS,
                "start_date": window[0][0], "end_date": window[-1][0],
                "breadth_change_percentage_points": breadth_change,
                "index_return_pct": index_return,
                "divergence_percentage_points": breadth_change - index_return,
                "calculation": "上涨比例百分点变化 − 指数百分比收益；仅为不同指标的探索性差值",
                "message": "探索性同日差异，不是投资信号或因果结论",
            }
            any_ready = True
        valuation = {}
        for metric in VALUATION_METRICS:
            series = _active_series(valuation_db, index_code, metric)
            pairs = _exact_pairs(breadth_by_date, series)
            monthly_audit = _monthly_audit(pairs)
            item = {
                "available": False,
                "common_dates": len(pairs),
                "required_common_dates": MIN_VALUATION_POINTS,
                "frequency": None if series is None else series.get("frequency"),
                "source_interface": None if series is None else series.get("source_interface"),
                "calculation": "Pearson相关系数：同日上涨比例与估值水平；不填充、不插值",
                **monthly_audit,
                "message": "严格同日的真实宽度与估值样本不足，不计算相关性",
            }
            if series is not None and series.get("frequency") not in ("daily", "monthly"):
                item["message"] = "估值频率不是已识别的日频或月频，拒绝联动"
            elif series is not None and series.get("frequency") == "monthly" and monthly_audit["duplicate_months"]:
                item["message"] = "月频估值同一自然月出现多条严格同日样本，拒绝混作月频"
            elif series is not None and series.get("frequency") == "monthly" and monthly_audit["missing_month_count"]:
                item["message"] = (
                    f"严格同日月频样本存在{monthly_audit['missing_month_count']}个缺失月份，"
                    "不使用跨年零星样本计算相关性"
                )
            elif series is not None and series.get("frequency") == "monthly" and monthly_audit["common_months"] < MIN_VALUATION_POINTS:
                item["message"] = "至少需要12个连续自然月的严格同日估值样本"
            elif series is not None and series.get("frequency") == "daily" and (
                    len(pairs) < MIN_VALUATION_POINTS or
                    not _dense([pair[0] for pair in pairs[-MIN_VALUATION_POINTS:]])):
                item["message"] = "日频估值至少需要12个稠密严格同日样本"
            elif len(pairs) >= MIN_VALUATION_POINTS:
                correlation = _correlation(
                    [pair[1] for pair in pairs], [pair[2] for pair in pairs]
                )
                if correlation is not None:
                    item.update({
                        "available": True,
                        "start_date": pairs[0][0], "end_date": pairs[-1][0],
                        "correlation": correlation,
                        "message": "仅为严格同日样本的探索性相关性，不前向填充月频估值",
                    })
                    any_ready = True
                else:
                    item["message"] = "样本无有效波动，无法计算相关性"
            valuation[metric] = item
        result["indices"][index_code] = {
            "index_close_source": None if index_series is None else index_series["source_interface"],
            "index_close_frequency": None if index_series is None else index_series.get("frequency"),
            "divergence": divergence,
            "valuation_relationship": valuation,
        }
    result["available"] = any_ready
    if any_ready:
        result["message"] = "仅显示达到真实同日样本门槛的探索性结果；其余项目保持不可用"
    return result
