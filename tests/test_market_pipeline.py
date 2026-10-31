from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from data_sources.history import DataSourceError, validate_history
from data_sources.market import (
    METHODOLOGY,
    cross_validate_sources,
    fetch_index_history,
    import_index_history,
    read_index_history,
    update_index_history,
    validate_index_history,
)
from data_sources.storage import connect


def daily_rows(start=date(2016, 1, 4), count=2600, base=3500.0):
    rows = []
    current = start
    while len(rows) < count:
        if current.weekday() < 5:
            rows.append((current.isoformat(), base + len(rows) * .25))
        current += timedelta(days=1)
    return rows


def fetched(rows):
    return {
        "index_code": "000300",
        "metric": "index_close",
        "rows": rows,
        "fetched_count": len(rows),
        "source_version": "test",
        "columns": ["日期", "开盘", "收盘", "最高", "最低"],
        "source_name": "东方财富 / AKShare",
        "source_interface": "ak.index_zh_a_hist",
        "methodology": METHODOLOGY,
        "series_id": "test_000300_daily_close_v1",
    }


class MarketPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "valuation.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_first_market_update_persists_index_close(self):
        rows = daily_rows(count=100)
        result = update_index_history("000300", db_path=self.db, fetcher=lambda *_: fetched(rows))
        stored, status = read_index_history(self.db, "000300")
        self.assertEqual(result["inserted"], 100)
        self.assertEqual(len(stored), 100)
        self.assertEqual(status["source_interface"], "ak.index_zh_a_hist")
        self.assertIn("不是 ETF", status["methodology"])
        self.assertEqual(status["frequency"], "daily")

    def test_repeated_market_update_is_idempotent(self):
        rows = daily_rows(count=100)
        fetcher = lambda *_: fetched(rows)
        update_index_history("000300", db_path=self.db, fetcher=fetcher)
        second = update_index_history("000300", db_path=self.db, fetcher=fetcher)
        with closing(connect(self.db)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM observations WHERE metric='index_close'").fetchone()[0]
        self.assertEqual(count, 100)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 0)

    def test_market_failure_preserves_existing_series(self):
        rows = daily_rows(count=100)
        update_index_history("000300", db_path=self.db, fetcher=lambda *_: fetched(rows))

        def fail(*_):
            raise DataSourceError("simulated market timeout")

        with self.assertRaises(DataSourceError):
            update_index_history("000300", db_path=self.db, fetcher=fail)
        stored, status = read_index_history(self.db, "000300")
        self.assertEqual(len(stored), 100)
        self.assertEqual(status["data_state"], "update_failed")
        self.assertIn("simulated market timeout", status["last_error"])

    def test_short_fallback_cannot_replace_complete_active_history(self):
        complete = daily_rows(count=2628)
        update_index_history("000300", db_path=self.db, fetcher=lambda *_: fetched(complete))
        short = daily_rows(start=date(2026, 10, 1), count=17, base=3900)
        candidate = {
            **fetched(short),
            "series_id": "tencent_akshare_000300_daily_close_v1",
            "source_name": "腾讯财经 / AKShare",
            "source_interface": "ak.stock_zh_index_daily_tx",
        }

        with self.assertRaisesRegex(DataSourceError, "候选行情历史不足"):
            update_index_history("000300", db_path=self.db, fetcher=lambda *_: candidate)

        stored, status = read_index_history(self.db, "000300")
        self.assertEqual(len(stored), 2628)
        self.assertEqual(status["series_id"], "test_000300_daily_close_v1")
        self.assertEqual(status["data_state"], "update_failed")
        self.assertIn("已保留原活动序列", status["last_error"])

    def test_market_column_change_is_rejected(self):
        provider = lambda *_: {
            "version": "test",
            "columns": ["日期", "最新价"],
            "records": [{"日期": "2026-01-05", "最新价": 4000}],
        }
        with self.assertRaisesRegex(DataSourceError, "列结构已变化"):
            fetch_index_history("000300", provider=provider, retries=1)

    def test_sina_columns_are_normalized_without_changing_semantics(self):
        provider = lambda *_: {
            "version": "test", "columns": ["date", "open", "close"],
            "records": [{"date": "2026-01-05", "open": 4000, "close": 4012.5}],
        }
        from unittest.mock import patch
        with patch("data_sources.market._call_provider", side_effect=lambda *args: provider()):
            result = fetch_index_history("000300", provider_name="sina", retries=1)
        self.assertEqual(result["rows"], [("2026-01-05", 4012.5)])
        self.assertEqual(result["source_interface"], "ak.stock_zh_index_daily")

    def test_cross_validation_records_five_real_dates_and_differences(self):
        rows = daily_rows(count=10)
        primary = {**fetched(rows), "source_interface": "primary"}
        secondary = {**fetched([(day, value + .01) for day, value in rows]), "source_interface": "secondary"}
        result = cross_validate_sources(primary, secondary)
        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(len(result["samples"]), 5)
        self.assertIn(result["status"], ("passed", "warning"))

    def test_auto_provider_falls_back_after_bounded_failure(self):
        from unittest.mock import patch
        calls = []

        def caller(provider_name, *_):
            calls.append(provider_name)
            if provider_name == "sina":
                raise DataSourceError("simulated upstream failure")
            return {
                "version": "test", "columns": ["date", "close"],
                "records": [{"date": "2026-01-05", "close": 4012.5}],
            }

        with patch("data_sources.market._call_provider", side_effect=caller):
            result = fetch_index_history("000300", provider_name="auto")
        self.assertEqual(calls, ["sina", "tencent"])
        self.assertEqual(result["provider"], "tencent")

    def test_official_provider_is_explicit_and_not_in_auto_order(self):
        from unittest.mock import patch
        from data_sources.market import PROVIDER_CHOICES, PROVIDER_ORDER
        self.assertIn("csindex_official", PROVIDER_CHOICES)
        self.assertNotIn("csindex_official", PROVIDER_ORDER)
        official = {
            "version": "test", "columns": ["日期", "指数代码", "收盘"],
            "records": [{"日期": "2026-01-05", "指数代码": "000300", "收盘": 4012.5}],
        }
        with patch("data_sources.csindex.fetch_official", return_value=official):
            result = fetch_index_history(
                "000300", start_date="20260101", end_date="20260131",
                provider_name="csindex_official", retries=1,
            )
        self.assertEqual(result["rows"], [("2026-01-05", 4012.5)])
        self.assertEqual(result["series_id"], "csindex_official_akshare_000300_daily_close_v1")

    def test_controlled_csv_import_keeps_declared_source_separate(self):
        rows = daily_rows(count=30)
        result = import_index_history(
            "000300", rows, "独立文件供应方", "已人工核对指数代码与收盘字段", db_path=self.db,
        )
        stored, status = read_index_history(self.db, "000300")
        self.assertEqual(result["count"], 30)
        self.assertEqual(len(stored), 30)
        self.assertEqual(status["source_interface"], "local.csv.index_close")
        self.assertIn("不标记为官方数据", status["methodology"])

    def test_market_validation_deduplicates_and_keeps_large_moves(self):
        quality = validate_index_history([
            ("2026-01-05", 4000), ("2026-01-05", 4010),
            ("2026-01-06", 5200), ("2026-01-07", 5180), ("2026-01-28", 5100),
        ], today=date(2026, 10, 31))
        self.assertEqual(quality["duplicate_dates"], 1)
        self.assertEqual(quality["shock_count"], 1)
        self.assertGreaterEqual(quality["missing_periods"], 1)
        self.assertEqual(quality["rows"][1]["value"], 5200)

    def test_ten_year_support_uses_calendar_date(self):
        almost = validate_history([
            ("2016-10-31", 10), ("2017-01-31", 10.1), ("2026-10-30", 12)
        ] + [(f"{year}-06-30", 10 + year % 3) for year in range(2017, 2026)], today=date(2026, 10, 31))
        exact = validate_history(daily_rows(start=date(2016, 10, 28), count=2610, base=10), today=date(2027, 12, 31))
        self.assertFalse(almost["window_support"]["10"])
        self.assertTrue(exact["window_support"]["10"])


if __name__ == "__main__":
    unittest.main()
