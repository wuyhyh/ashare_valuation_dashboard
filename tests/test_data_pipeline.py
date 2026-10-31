from __future__ import annotations

from datetime import date
from contextlib import closing
import sqlite3
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import app
from data_sources.history import (
    DataSourceError,
    SOURCE_SPECS,
    fetch_history,
    get_update_status,
    import_history,
    read_active_history,
    update_history,
    validate_history,
)
from data_sources.storage import connect, ensure_schema


def monthly_rows(start_year=2015, months=120, base=12.0):
    rows = []
    for offset in range(months):
        month_index = start_year * 12 + offset
        year, month_zero = divmod(month_index, 12)
        rows.append((date(year, month_zero + 1, 28).isoformat(), base + (offset % 17) * .03))
    return rows


def fetched(metric, rows):
    spec = SOURCE_SPECS[metric]
    return {
        "index_code": "000300", "metric": metric, "rows": rows,
        "fetched_count": len(rows), "source_version": "test",
        "columns": ["日期", spec["value_column"]],
        "source_name": spec["source_name"], "source_interface": spec["source_interface"],
        "methodology": spec["methodology"],
        "series_id": f"test_000300_{metric}_{spec['series_suffix']}",
    }


class DataPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "valuation.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_first_empty_database_fetches_hs300_pe_ttm(self):
        rows = monthly_rows()
        result = update_history("000300", "pe_ttm", db_path=self.db,
                                fetcher=lambda *_: fetched("pe_ttm", rows))
        stored, status = read_active_history(self.db, "000300", "pe_ttm")
        self.assertEqual(result["inserted"], len(rows))
        self.assertEqual(len(stored), len(rows))
        self.assertEqual(status["source_interface"], "ak.stock_index_pe_lg")
        self.assertEqual(status["frequency"], "monthly")

    def test_fetches_hs300_pb_without_using_equal_weight_column(self):
        rows = monthly_rows(base=1.3)
        update_history("000300", "pb", db_path=self.db, fetcher=lambda *_: fetched("pb", rows))
        stored, status = read_active_history(self.db, "000300", "pb")
        self.assertAlmostEqual(stored[0]["value"], 1.3)
        self.assertIn("非等权", status["methodology"])
        self.assertEqual(status["source_interface"], "ak.stock_index_pb_lg")

    def test_repeated_update_is_idempotent(self):
        rows = monthly_rows()
        fetcher = lambda *_: fetched("pe_ttm", rows)
        update_history("000300", "pe_ttm", db_path=self.db, fetcher=fetcher)
        second = update_history("000300", "pe_ttm", db_path=self.db, fetcher=fetcher)
        with closing(connect(self.db)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        self.assertEqual(count, len(rows))
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["unchanged"], len(rows))

    def test_network_failure_preserves_existing_history(self):
        rows = monthly_rows()
        update_history("000300", "pe_ttm", db_path=self.db,
                       fetcher=lambda *_: fetched("pe_ttm", rows))

        def fail(*_):
            raise DataSourceError("simulated network timeout")

        with self.assertRaises(DataSourceError):
            update_history("000300", "pe_ttm", db_path=self.db, fetcher=fail)
        stored, status = read_active_history(self.db, "000300", "pe_ttm")
        self.assertEqual(len(stored), len(rows))
        self.assertEqual(status["data_state"], "update_failed")
        self.assertIn("simulated network timeout", status["last_error"])

    def test_changed_upstream_columns_raise_clear_error(self):
        provider = lambda *_: {
            "version": "test", "columns": ["日期", "静态市盈率"],
            "records": [{"日期": "2026-01-01", "静态市盈率": 12.0}],
        }
        with self.assertRaisesRegex(DataSourceError, "列结构已变化"):
            fetch_history("000300", "pe_ttm", provider=provider, retries=1)

    def test_insufficient_coverage_has_reliable_status(self):
        rows = monthly_rows(start_year=2025, months=22)[-12:]
        update_history("000300", "pb", db_path=self.db, fetcher=lambda *_: fetched("pb", rows))
        status = get_update_status(db_path=self.db)["000300:pb"]
        self.assertEqual(status["data_state"], "insufficient")
        self.assertFalse(status["window_support"]["3"])
        self.assertTrue(any("覆盖不足" in warning for warning in status["warnings"]))

    def test_page_refresh_reads_persisted_active_sqlite_series(self):
        rows = monthly_rows()
        update_history("000300", "pe_ttm", db_path=self.db,
                       fetcher=lambda *_: fetched("pe_ttm", rows))
        first, _ = read_active_history(self.db, "000300", "pe_ttm")
        second, _ = read_active_history(Path(str(self.db)), "000300", "pe_ttm")
        self.assertEqual(first, second)
        self.assertEqual(first[-1]["date"], rows[-1][0])

    def test_different_methodologies_are_not_concatenated(self):
        csv_rows = monthly_rows(start_year=2024, months=12, base=20)
        import_history("000300", "pe_ttm", csv_rows, "user", db_path=self.db)
        upstream_rows = monthly_rows(start_year=2015, months=120, base=12)
        update_history("000300", "pe_ttm", db_path=self.db,
                       fetcher=lambda *_: fetched("pe_ttm", upstream_rows))
        active, status = read_active_history(self.db, "000300", "pe_ttm")
        with closing(connect(self.db)) as connection:
            series_count = connection.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        self.assertEqual(series_count, 2)
        self.assertEqual(len(active), len(upstream_rows))
        self.assertEqual(status["source_interface"], "ak.stock_index_pe_lg")

    def test_validation_deduplicates_flags_gaps_and_keeps_shocks(self):
        quality = validate_history([
            ("2024-01-31", 10), ("2024-01-31", 11),
            ("2024-03-31", 20), ("2024-04-30", 21), ("bad", 12), ("2024-05-31", None),
        ], today=date(2026, 1, 1))
        self.assertEqual(quality["duplicate_dates"], 1)
        self.assertEqual(quality["missing_periods"], 1)
        self.assertEqual(quality["shock_count"], 1)
        self.assertEqual(quality["rows"][-1]["value"], 21)

    def test_legacy_database_is_backed_up_and_migrated(self):
        connection = sqlite3.connect(self.db)
        connection.execute("""CREATE TABLE observations (
            index_code TEXT, metric TEXT, date TEXT, value REAL, source TEXT, fetched_at TEXT,
            PRIMARY KEY(index_code,metric,date))""")
        connection.execute("INSERT INTO observations VALUES(?,?,?,?,?,?)",
                           ("000300", "pe_ttm", "2025-01-01", 12.3, "legacy", "2025-01-02T00:00:00+00:00"))
        connection.commit()
        connection.close()
        backup = ensure_schema(self.db)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        rows, _ = read_active_history(self.db, "000300", "pe_ttm")
        self.assertEqual(len(rows), 1)

    def test_cli_update_failure_returns_nonzero(self):
        with patch.object(app, "update_akshare", side_effect=RuntimeError("offline")), \
             patch.object(sys, "argv", ["app.py", "update", "--index", "000300", "--metric", "pe_ttm"]):
            self.assertEqual(app.main(), 1)


if __name__ == "__main__":
    unittest.main()
