"""Offline point-in-time market breadth tests; NOT live trading-day evidence."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app

import pytest

import breadth


def q(code, name, change, price=10.0, turnover=1000.0):
    return {"代码": code, "名称": name, "涨跌幅": change, "最新价": price, "成交额": turnover}


def official_pool_for(rows):
    return {
        row["代码"]: {"exchange": breadth.mainboard_exchange(row["代码"]), "name": row["名称"]}
        for row in rows
    }


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


def test_tencent_schema_is_canonicalized_with_explicit_turnover_unit():
    rows = breadth.adapt_spot_records([{
        "code": "sh600000", "name": "浦发银行", "zxj": "10.25",
        "zdf": "1.25", "turnover": "123.45",
    }], "tencent")
    assert rows == [{"代码": "600000", "名称": "浦发银行", "最新价": "10.25", "涨跌幅": "1.25", "成交额": 1_234_500.0}]


def test_official_pool_coverage_is_recorded_and_low_coverage_rejected():
    rows = [q("600001", "沪市", 1), q("000001", "深市", -1)]
    capture = breadth.create_capture(
        rows, source_name="test", source_interface="fixture", min_identified=1,
        official_pool=official_pool_for(rows),
    )
    audit = capture["quality"]["official_pool"]
    assert audit["coverage_ratio"] == 1
    assert audit["exchange"]["SH"]["official_count"] == 1
    incomplete = {**official_pool_for(rows), "600002": {"exchange": "SH", "name": "缺失"}}
    with pytest.raises(breadth.BreadthError, match="官方主板名录覆盖不足"):
        breadth.create_capture(
            rows, source_name="test", source_interface="fixture", min_identified=1,
            official_pool=incomplete,
        )


def test_auto_provider_falls_back_and_records_failed_attempt():
    def fetcher(provider):
        if provider == "eastmoney":
            return {"ok": False, "error": "rate limited"}
        return {
            "ok": True, "version": "offline-test", "columns": ["code", "name", "zxj", "zdf", "turnover"],
            "records": [{"code": "sh600000", "name": "浦发银行", "zxj": 10, "zdf": 1, "turnover": 5}],
        }
    result = breadth.fetch_spot("auto", fetcher=fetcher)
    assert result["provider"] == "tencent"
    assert result["source_interface"] == "ak.stock_zh_a_spot_tx"
    assert result["attempts"][0] == {"provider": "eastmoney", "error": "rate limited"}


def test_native_provider_column_change_is_explicit_and_can_fall_back():
    def fetcher(provider):
        if provider == "eastmoney":
            return {"ok": True, "version": "offline-test", "columns": ["代码"], "records": [{"代码": "600000"}]}
        return {
            "ok": True, "version": "offline-test", "columns": ["code", "name", "zxj", "zdf", "turnover"],
            "records": [{"code": "sh600000", "name": "浦发银行", "zxj": 10, "zdf": 1, "turnover": 5}],
        }
    result = breadth.fetch_spot("auto", fetcher=fetcher)
    assert result["provider"] == "tencent"
    assert "字段发生变化" in result["attempts"][0]["error"]


def test_extreme_changes_and_official_name_mismatches_are_audited_not_deleted():
    rows = [q("600001", "名称A", 35), q("000001", "名称B", -1)]
    pool = official_pool_for(rows)
    pool["600001"]["name"] = "另一个名称"
    capture = breadth.create_capture(
        rows, source_name="test", source_interface="fixture", min_identified=1,
        official_pool=pool,
    )
    assert capture["quality"]["extreme_change_count"] == 1
    assert capture["quality"]["official_pool"]["name_mismatch_count"] == 1
    assert len(capture["quotes"]) == 2


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


def test_breadth_http_api_is_read_only_and_never_returns_demo():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "breadth.sqlite3"
        with patch.object(app, "BREADTH_DB", db):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/api/breadth"
            try:
                with urllib.request.urlopen(url) as response:
                    empty = json.load(response)
                assert not empty["available"]
                assert not db.exists()
                snapshot = breadth.create_capture(
                    [q("600001", "样本", 1)], source_interface="fixture",
                    source_name="offline-test", min_identified=1,
                )
                breadth.persist_capture(snapshot, db)
                with urllib.request.urlopen(url) as response:
                    payload = json.load(response)
                assert payload["available"]
                assert payload["groups"]["ALL_all"]["advances"] == 1
                assert payload["capture"]["trading_date"] is None
                assert not payload["historical_trend_ready"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


def test_breadth_worker_drains_large_queue_before_join():
    events = []

    class FakeQueue:
        def get(self, timeout):
            events.append("get")
            return {
                "ok": True, "version": "fixture", "columns": list(breadth.REQUIRED_COLUMNS),
                "records": [q("600001", "测试", 1)],
            }
        def close(self):
            events.append("close")

    class FakeProcess:
        def start(self):
            events.append("start")
        def join(self, timeout):
            events.append("join")
        def is_alive(self):
            return False

    class FakeContext:
        def Queue(self, maxsize):
            return FakeQueue()
        def Process(self, target, args, daemon):
            return FakeProcess()

    with patch.object(breadth.multiprocessing, "get_context", return_value=FakeContext()):
        rows, version = breadth.fetch_spot_em(timeout_seconds=3)
    assert len(rows) == 1 and version == "fixture"
    assert events.index("get") < events.index("join")
    assert "close" in events


def test_shortened_subsequent_breadth_snapshot_rejected_before_database_write():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "breadth.sqlite3"
        source = "ak.stock_zh_a_spot_em"
        def make_rows(n):
            return [q(f"{600000 + i:06d}", "真实来源测试（离线）", 0.5)
                    for i in range(n)]
        old = breadth.create_capture(make_rows(1100), source_interface=source,
                                     source_name="fixture", min_identified=1000)
        breadth.persist_capture(old, db)
        truncated = breadth.create_capture(make_rows(800), source_interface=source,
                                           source_name="fixture", min_identified=1)
        with pytest.raises(breadth.BreadthError, match="截断"):
            breadth.validate_against_previous(truncated, db)
        assert breadth.latest_report(db)["capture"]["capture_id"] == old["capture_id"]


def test_other_provider_does_not_receive_comparability_guard_without_cross_check():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "breadth.sqlite3"
        old = breadth.create_capture([q("600001", "样本", 1)], source_interface="fixture-A",
                                     source_name="offline-test", min_identified=1)
        breadth.persist_capture(old, db)
        new = breadth.create_capture([q("600001", "样本", 1)], source_interface="fixture-B",
                                     source_name="offline-test", min_identified=1)
        breadth.validate_against_previous(new, db)
