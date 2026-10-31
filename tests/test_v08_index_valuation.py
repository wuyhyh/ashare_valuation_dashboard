"""Offline contract tests for the four Legu index valuation feeds.

These fixtures do not establish that AKShare is reachable on a real network.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from data_sources import history as history_module
from data_sources.history import (
    DataSourceError,
    INDEX_NAMES,
    SOURCE_SPECS,
    fetch_history,
    cross_validate_valuation_identity,
    read_active_history,
    update_history,
)


EXPECTED = {
    "000016": "上证50",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
}
DATES = ("2025-01-31", "2025-02-28", "2025-03-31")


def synthetic_provider(symbol: str, metric: str, _timeout: int) -> dict:
    spec = SOURCE_SPECS[metric]
    return {
        "version": "offline-test",
        "columns": ["日期", "指数", spec["value_column"]],
        "records": [
            {"日期": day, "指数": 3500.0 + i, spec["value_column"]: 12.0 + i}
            for i, day in enumerate(DATES)
        ],
    }


class FourIndexValuationContractTests(unittest.TestCase):
    def test_supported_symbols_match_dashboard(self):
        self.assertEqual(INDEX_NAMES, EXPECTED)

    def test_all_eight_index_metric_combinations_use_correct_symbol_and_column(self):
        unique_series_ids = set()
        for code, index_name in EXPECTED.items():
            for metric in ("pe_ttm", "pb"):
                with self.subTest(index=code, metric=metric):
                    observed = []

                    def provider(symbol, requested_metric, timeout):
                        observed.append((symbol, requested_metric, timeout))
                        return synthetic_provider(symbol, requested_metric, timeout)

                    fetched = fetch_history(code, metric, provider=provider)
                    self.assertEqual(observed, [(index_name, metric, 35)])
                    self.assertEqual(fetched["rows"], [
                        (day, 12.0 + i) for i, day in enumerate(DATES)
                    ])
                    self.assertEqual(
                        fetched["source_interface"],
                        SOURCE_SPECS[metric]["source_interface"],
                    )
                    self.assertEqual(fetched["methodology"], SOURCE_SPECS[metric]["methodology"])
                    self.assertEqual(fetched["upstream_index_field"]["kind"], "numeric_level")
                    self.assertEqual(fetched["upstream_index_field"]["sample_count"], len(DATES))
                    unique_series_ids.add(fetched["series_id"])
        self.assertEqual(len(unique_series_ids), 8)

    def test_all_eight_series_roundtrip_in_separate_sqlite_records(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "valuation.sqlite3"
            for code in EXPECTED:
                for metric in ("pe_ttm", "pb"):
                    with self.subTest(index=code, metric=metric):
                        result = update_history(
                            code,
                            metric,
                            db_path=db,
                            fetcher=lambda index_code, chosen_metric: fetch_history(
                                index_code,
                                chosen_metric,
                                provider=synthetic_provider,
                            ),
                        )
                        self.assertEqual(result["count"], len(DATES))
                        rows, status = read_active_history(db, code, metric)
                        self.assertEqual([item["date"] for item in rows], list(DATES))
                        self.assertEqual(len(rows), len(DATES))
                        self.assertEqual(status["source_interface"], SOURCE_SPECS[metric]["source_interface"])

    def test_empty_or_changed_schema_rejected_without_fabrication(self):
        for code in EXPECTED:
            for metric in ("pe_ttm", "pb"):
                with self.subTest(index=code, metric=metric):
                    def broken_provider(symbol, requested_metric, timeout):
                        return {
                            "version": "offline-test",
                            "columns": ["日期", "另一个指标"],
                            "records": [{"日期": DATES[0], "另一个指标": 1.0}],
                        }

                    with self.assertRaises(DataSourceError):
                        fetch_history(code, metric, provider=broken_provider, retries=1)

    def test_worker_drains_queue_before_join(self):
        events = []

        class FakeQueue:
            def get(self, timeout):
                events.append("get")
                return {"ok": True, "records": [], "columns": [], "version": "fixture"}

            def close(self):
                events.append("queue_close")

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

        with patch.object(history_module.multiprocessing, "get_context", return_value=FakeContext()):
            result = history_module._call_akshare("沪深300", "pe_ttm", timeout_seconds=5)
        self.assertTrue(result["ok"])
        self.assertLess(events.index("get"), events.index("join"))
        self.assertIn("queue_close", events)

    def test_unknown_index_not_accepted(self):
        with self.assertRaises(DataSourceError):
            fetch_history("399006", "pe_ttm", provider=synthetic_provider)

    def test_numeric_index_level_can_be_cross_checked_against_close(self):
        valuation_levels = [(day, 3500.0 + offset) for offset, day in enumerate(DATES)]
        market_rows = [
            {"date": day, "value": 3500.0 + offset, "source": "official"}
            for offset, day in enumerate(DATES)
        ]
        result = cross_validate_valuation_identity(valuation_levels, market_rows)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["common_count"], len(DATES))
        self.assertEqual(result["max_abs_difference_pct"], 0)


if __name__ == "__main__":
    unittest.main()
