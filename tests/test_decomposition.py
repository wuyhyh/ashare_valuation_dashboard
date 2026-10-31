"""Deterministic decomposition fixtures; never used as live market data."""
from datetime import date, timedelta
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decomposition import analyze, subtract_calendar_months


def fixture(years=4, source="fixture"):
    end = date(2026, 10, 30)
    start = end - timedelta(days=round(365.25 * years))
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    denominator = len(days) - 1
    prices = [
        {"date": value, "value": 3000 * math.exp(math.log(1.2) * index / denominator), "source": source}
        for index, value in enumerate(days)
    ]
    multiples = [
        {"date": value, "value": 12 * math.exp(math.log(1.5) * index / denominator), "source": source}
        for index, value in enumerate(days)
    ]
    return prices, multiples


def test_date_shift_uses_calendar_months():
    assert subtract_calendar_months("2024-02-29", 12) == "2023-02-28"
    assert subtract_calendar_months("2026-10-30", 120) == "2016-10-30"


def test_exact_log_attribution():
    prices, multiples = fixture()
    result = analyze(prices, multiples, horizon="3y")
    contribution = result["log_contributions_pp"]
    assert abs(contribution["multiple"] + contribution["implied"] - contribution["close"]) < 1e-10
    assert result["changes_pct"]["close"] > 0
    assert result["changes_pct"]["multiple"] > 0
    assert result["changes_pct"]["implied"] < 0
    assert result["series"][0]["index"] == 100
    assert result["matched_count"] > 700


def test_pb_uses_proxy_label_not_actual_book_value():
    prices, multiples = fixture()
    result = analyze(prices, multiples, metric="pb", horizon="1y")
    assert result["label"] == "隐含净资产代理"


def test_ten_year_history_cannot_be_faked():
    prices, multiples = fixture()
    with pytest.raises(ValueError, match="历史覆盖不足"):
        analyze(prices, multiples, horizon="10y")


def test_sparse_common_dates_are_not_interpolated():
    prices, multiples = fixture()
    multiples = [row for index, row in enumerate(multiples) if index % 7 == 0]
    with pytest.raises(ValueError, match="同日有效数据"):
        analyze(prices, multiples, horizon="1y")


def test_nonmatching_dates_fail():
    prices, _ = fixture()
    multiples = [{"date": "2000-01-01", "value": 12, "source": "fixture"}]
    with pytest.raises(ValueError, match="没有同一交易日"):
        analyze(prices, multiples, horizon="1y")


def test_mixed_source_series_is_rejected():
    prices, multiples = fixture()
    multiples[-1]["source"] = "another-source"
    with pytest.raises(ValueError, match="混合"):
        analyze(prices, multiples, horizon="1y")


def test_large_common_date_gap_is_rejected():
    prices, multiples = fixture()
    prices = [row for row in prices if not ("2026-03-01" <= row["date"] <= "2026-05-01")]
    with pytest.raises(ValueError, match="缺口"):
        analyze(prices, multiples, horizon="1y")


def test_asof_never_uses_future_rows():
    prices, multiples = fixture()
    result = analyze(prices, multiples, horizon="1y", asof="2026-06-30")
    assert result["end"] <= "2026-06-30"
    assert all(row["date"] <= "2026-06-30" for row in result["series"])


def test_dashboard_api_adapter_reads_current_active_series(tmp_path, monkeypatch):
    import app as dashboard
    from data_sources.history import import_history

    prices, multiples = fixture()
    database = tmp_path / "adapter.sqlite3"
    import_history("000300", "index_close", prices, "fixture market", db_path=database)
    import_history("000300", "pe_ttm", multiples, "fixture valuation", db_path=database)
    monkeypatch.setattr(dashboard, "DB", database)

    result = dashboard.get_decomposition_data("000300", "pe_ttm", "3y")
    assert result["name"] == "沪深300"
    assert result["matched_count"] > 700
    assert result["series"][0]["date"] == result["start"]
