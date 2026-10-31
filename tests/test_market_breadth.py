"""Offline point-in-time market breadth tests; NOT live trading-day evidence."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile

import pytest

import breadth


def q(code, name, change, price=10.0, turnover=1000.0):
    return {"代码": code, "名称": name, "涨跌幅": change, "最新价": price, "成交额": turnover}


def test_mainboard_classification_boundary():
    assert breadth.mainboard_exchange("600000") == "SH"
    assert breadth.mainboard_exchange("605999") == "SH"
    assert breadth.mainboard_exchange("000001") == "SZ"
    assert breadth.mainboard_exchange("002475") == "SZ"
    assert breadth.mainboard_exchange("003001") == "SZ"
    for code in ("300001", "688001", "920001", "830000", "900001", "200001", "399001", "60000", "006000"):
        assert breadth.mainboard_exchange(code) is None


def test_normalization_does_not_count_no_trade_as_flat():
    rows = [q("600000", "浦发银行", 1.2), q("000001", "平安银行", -2.0),
            q("002001", "ST测试", 0), q("603001", "普通停牌", 0, turnover=0),
            q("605001", "未能报价", "-", price="-", turnover="-"),
            q("300001", "创业板", 8)]
    quotes, checks = breadth.normalize_quotes(rows)
    result = breadth.summarize(quotes)
    assert checks["not_mainboard"] == 1
    assert result["identified_mainboard"] == 5
    assert result["advances"] == 1 and result["declines"] == 1 and result["unchanged"] == 1
    assert result["zero_turnover_count"] == 1
    assert result["missing_quote_count"] == 1
    assert result["traded_count"] == 3
    assert result["advance_ratio"] == pytest.approx(1/3)
    assert result["median_change_pct"] == 0
    assert result["turnover_weighted_change_pct"] == pytest.approx((-0.8)/3)


def test_exclude_st_is_explicit_and_exchange_filters_are_distinct():
    rows = [q("600001", "*ST样本", -4.2), q("600002", "正常", 1.2),
            q("000002", "ST深市", 1.0), q("002002", "深市下跌", -2.0)]
    quotes, _ = breadth.normalize_quotes(rows)
    assert breadth.summarize(quotes, exchange="SH")["advances"] == 1
    assert breadth.summarize(quotes, exchange="SZ")["advances"] == 1
    no_st = breadth.summarize(quotes, exclude_risk=True)
    assert no_st["selected_count"] == 2
    assert no_st["risk_warning"] == 2
    assert no_st["excluded_risk"] == 2
    assert no_st["advances"] == 1 and no_st["declines"] == 1
    assert breadth.summarize(quotes)["selected_count"] == 4


def test_bad_values_or_duplicate_codes_are_rejected_or_excluded():
    values = [q("600001", "银行", "nan"), q("600002", "银行", "inf"),
              q("600003", "银行", 0, price=0), q("000001", "银行", 2)]
    quotes, _ = breadth.normalize_quotes(values)
    assert breadth.summarize(quotes)["missing_quote_count"] == 3
    with pytest.raises(breadth.BreadthError, match="重复"):
        breadth.normalize_quotes([q("600001", "测试", 1), q("600001", "测试2", -1)])
    with pytest.raises(breadth.BreadthError, match="字段缺失"):
        breadth.normalize_quotes([{"代码": "600001"}])


def test_capture_time_is_not_mislabelled_as_trading_date():
    rows = [q("600001", "银行", 1), q("000001", "银行", -1)]
    captured = datetime(2026, 10, 31, 17, 20, tzinfo=timezone.utc)  # Nov 1 in China
    snapshot = breadth.create_capture(rows, source_name="offline-test", source_interface="fixture",
                                      captured_at=captured, min_identified=1)
    assert snapshot["capture_local_date"] == "2026-11-01"
    assert snapshot["trading_date"] is None
    assert not snapshot["trading_date_verified"]
    assert "不能替代交易日" in snapshot["quote_date_note"]


def test_small_or_truncated_capture_is_never_persisted():
    rows = [q("600001", "测试", 1)]
    with pytest.raises(breadth.BreadthError, match="至少需要"):
        breadth.create_capture(rows, source_name="test", source_interface="fixture")
    with pytest.raises(breadth.BreadthError, match="有效成交"):
        breadth.create_capture([q("600001", "测试", 1, turnover=0),
                                q("000001", "测试", 2, turnover=0)],
                               source_name="test", source_interface="fixture", min_identified=1)


def test_sqlite_keeps_distinct_capture_times_and_quotes():
    rows = [q("600001", "银行", 1), q("000001", "银行", -1)]
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "breadth.sqlite3"
        empty = breadth.latest_report(db)
        assert not empty["available"]
        assert not db.exists()
        first = breadth.create_capture(rows, source_name="offline-test", source_interface="fixture",
                                       captured_at=datetime(2026, 11, 1, 1, tzinfo=timezone.utc),
                                       min_identified=1)
        second = breadth.create_capture(rows, source_name="offline-test", source_interface="fixture",
                                        captured_at=datetime(2026, 11, 1, 2, tzinfo=timezone.utc),
                                        min_identified=1)
        breadth.persist_capture(first, db)
        breadth.persist_capture(second, db)
        report = breadth.latest_report(db)
        assert report["available"]
        assert report["capture"]["capture_id"] == second["capture_id"]
        assert report["groups"]["ALL_all"]["traded_count"] == 2
        assert len(report["history"]) == 2
        assert not report["historical_trend_ready"]
        assert report["capture"]["trading_date"] is None


def test_bad_capture_does_not_change_existing_history():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "breadth.sqlite3"
        good = breadth.create_capture([q("600001", "银行", 1)], source_interface="fixture",
                                      source_name="test", min_identified=1)
        breadth.persist_capture(good, db)
        with pytest.raises(breadth.BreadthError):
            breadth.create_capture([], source_interface="fixture", source_name="test", min_identified=1)
        assert breadth.latest_report(db)["capture"]["capture_id"] == good["capture_id"]
