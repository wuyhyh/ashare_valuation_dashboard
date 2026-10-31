"""Index-level valuation decomposition, deliberately not accounting attribution.

P_t = M_t * X_t, where X_t := index close / PE TTM or PB. Therefore
ln(P1/P0) = ln(M1/M0) + ln(X1/X0) on identical observation dates.
The implied X proxy is not constituent profit, EPS, or book value.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
import math
from statistics import median


ALLOWED_HORIZONS = {"1y": 12, "3y": 36, "5y": 60, "10y": 120}


def subtract_calendar_months(iso_date: str, months: int) -> str:
    value = date.fromisoformat(iso_date)
    absolute_month = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(absolute_month, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, monthrange(year, month)[1])).isoformat()


def _source(rows):
    sources = {row.get("source", "") for row in rows}
    if len(sources) != 1 or not next(iter(sources), ""):
        return None
    return next(iter(sources))


def _only_positive(rows, key="value"):
    result = {}
    for row in rows:
        try:
            observation_date = str(row["date"])
            date.fromisoformat(observation_date)
            value = float(row[key])
            if math.isfinite(value) and value > 0:
                result[observation_date] = {
                    "date": observation_date,
                    "value": value,
                    "source": row.get("source", ""),
                }
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def analyze(prices, valuations, metric="pe_ttm", horizon="3y", index_code="000300", asof=None):
    """Analyze one index using positive observations on identical real dates only."""
    if horizon not in ALLOWED_HORIZONS:
        raise ValueError("不支持的归因区间")
    if metric not in ("pe_ttm", "pb"):
        raise ValueError("不支持的估值指标")

    price_map = _only_positive(prices)
    multiple_map = _only_positive(valuations)
    if not price_map or not multiple_map:
        raise ValueError("指数点位和估值序列均需真实数据；请先更新历史数据")

    price_source = _source(list(price_map.values()))
    valuation_source = _source(list(multiple_map.values()))
    if not price_source or not valuation_source:
        raise ValueError("同一序列混合了不同数据来源，无法保证归因口径一致；请先核对来源")

    common_dates = sorted(set(price_map).intersection(multiple_map))
    if asof:
        date.fromisoformat(asof)
        common_dates = [value for value in common_dates if value <= asof]
    if not common_dates:
        raise ValueError("指数和估值没有同一交易日的数据，无法归因")

    end = common_dates[-1]
    target = subtract_calendar_months(end, ALLOWED_HORIZONS[horizon])
    candidates = [
        value for value in common_dates
        if value >= target and (date.fromisoformat(value) - date.fromisoformat(target)).days <= 7
    ]
    if not candidates:
        raise ValueError(f"{horizon} 历史覆盖不足或起点附近没有同日观测（目标 {target}）")

    start = candidates[0]
    dates = [value for value in common_dates if start <= value <= end]
    duration = (date.fromisoformat(end) - date.fromisoformat(start)).days
    minimum_samples = max(20, int(duration * 0.30))
    if len(dates) < minimum_samples:
        raise ValueError(f"同日有效数据仅 {len(dates)} 条（至少需要 {minimum_samples} 条）；无法进行日频归因")

    gaps = [
        (date.fromisoformat(current) - date.fromisoformat(previous)).days
        for previous, current in zip(dates, dates[1:])
    ]
    if gaps and (median(gaps) > 4 or max(gaps) > 30):
        raise ValueError("归因区间存在过大的共同日期缺口；请核查估值/行情历史数据")

    price_start, price_end = price_map[start]["value"], price_map[end]["value"]
    multiple_start, multiple_end = multiple_map[start]["value"], multiple_map[end]["value"]
    implied_start, implied_end = price_start / multiple_start, price_end / multiple_end
    log_price = math.log(price_end / price_start)
    log_multiple = math.log(multiple_end / multiple_start)
    log_implied = math.log(implied_end / implied_start)
    warnings = [
        "这是一项指数层面的数学恒等式分解；隐含代理值不能等同于真实利润、EPS或净资产。"
        "指数成分、权重、除数变化以及不同来源的估值口径会影响解释。"
    ]
    lag_days = (date.today() - date.fromisoformat(end)).days
    if lag_days > 7:
        warnings.append(f"共同数据截止日距今天已有 {lag_days} 个自然日，结果可能过期。")
    if price_source != valuation_source:
        warnings.append("指数点位与估值来自不同上游接口，计算前需人工确认指数定义、复权和估值口径。")

    label = "隐含盈利代理" if metric == "pe_ttm" else "隐含净资产代理"
    series = []
    for observation_date in dates:
        price = price_map[observation_date]["value"]
        multiple = multiple_map[observation_date]["value"]
        series.append({
            "date": observation_date,
            "index": round(100 * price / price_start, 6),
            "multiple": round(100 * multiple / multiple_start, 6),
            "implied": round(100 * (price / multiple) / implied_start, 6),
        })

    return {
        "index": index_code,
        "metric": metric,
        "horizon": horizon,
        "start": start,
        "end": end,
        "target": target,
        "matched_count": len(dates),
        "price_source": price_source,
        "valuation_source": valuation_source,
        "lag_days": lag_days,
        "label": label,
        "initial": {"close": price_start, "multiple": multiple_start, "implied": implied_start},
        "final": {"close": price_end, "multiple": multiple_end, "implied": implied_end},
        "changes_pct": {
            "close": (price_end / price_start - 1) * 100,
            "multiple": (multiple_end / multiple_start - 1) * 100,
            "implied": (implied_end / implied_start - 1) * 100,
        },
        "log_contributions_pp": {
            "close": 100 * log_price,
            "multiple": 100 * log_multiple,
            "implied": 100 * log_implied,
        },
        "method": "log-return, exact: ln(P1/P0)=ln(M1/M0)+ln((P1/M1)/(P0/M0))",
        "warnings": warnings,
        "series": series,
    }
