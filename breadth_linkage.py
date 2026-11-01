"""Evidence-gated breadth linkage using exact real observation dates only."""
from __future__ import annotations

from contextlib import closing
from datetime import date
import math
from pathlib import Path
import sqlite3

from breadth_history import breadth_trend


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


def breadth_linkage(
    breadth_db: str | Path,
    valuation_db: str | Path,
    *,
    scope: str = "SH_SZ_ALL",
    exclude_st: bool = False,
    limit: int = 180,
) -> dict:
    """Compare verified breadth with active index/valuation series on exact dates.

    No date is shifted, forward-filled or interpolated. Results remain unavailable
    until their explicit minimum sample and density requirements are satisfied.
    """
    trend = breadth_trend(breadth_db, scope=scope, exclude_st=exclude_st, limit=limit)
    result = {
        "available": False,
        "scope": scope,
        "exclude_st": exclude_st,
        "breadth_points": trend["point_count"],
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
                "message": "探索性同日差异，不是投资信号或因果结论",
            }
            any_ready = True
        valuation = {}
        for metric in VALUATION_METRICS:
            series = _active_series(valuation_db, index_code, metric)
            pairs = _exact_pairs(breadth_by_date, series)
            item = {
                "available": False,
                "common_dates": len(pairs),
                "required_common_dates": MIN_VALUATION_POINTS,
                "frequency": None if series is None else series.get("frequency"),
                "message": "严格同日的真实宽度与估值样本不足，不计算相关性",
            }
            if series is not None and series.get("frequency") not in ("daily", "monthly"):
                item["message"] = "估值频率不是已识别的日频或月频，拒绝联动"
            elif len(pairs) >= MIN_VALUATION_POINTS:
                correlation = _correlation(
                    [pair[1] for pair in pairs], [pair[2] for pair in pairs]
                )
                if correlation is not None:
                    item.update({
                        "available": True,
                        "start_date": pairs[0][0], "end_date": pairs[-1][0],
                        "correlation": correlation,
                        "source_interface": series["source_interface"],
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
