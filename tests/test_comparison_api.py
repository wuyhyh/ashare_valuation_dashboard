from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from data_sources.history import SOURCE_SPECS, import_history, update_history


def monthly_rows(start_year=2015, months=142, base=12.0):
    rows = []
    for offset in range(months):
        month_index = start_year * 12 + offset
        year, month_zero = divmod(month_index, 12)
        rows.append((date(year, month_zero + 1, 28).isoformat(), base + (offset % 19) * .04))
    return rows


def fetched(index_code, metric, rows):
    spec = SOURCE_SPECS[metric]
    return {
        "index_code": index_code,
        "metric": metric,
        "rows": rows,
        "fetched_count": len(rows),
        "source_version": "test",
        "columns": ["日期", spec["value_column"]],
        "source_name": spec["source_name"],
        "source_interface": spec["source_interface"],
        "methodology": spec["methodology"],
        "series_id": f"test_{index_code}_{metric}_{spec['series_suffix']}",
    }


class ComparisonApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "valuation.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_four_real_series_use_latest_common_date(self):
        codes = ("000016", "000300", "000905", "000852")
        rows = monthly_rows()
        for offset, code in enumerate(codes):
            series_rows = [(day, value + offset * 3) for day, value in rows]
            update_history(code, "pe_ttm", db_path=self.db,
                           fetcher=lambda *_args, c=code, r=series_rows: fetched(c, "pe_ttm", r))
        with patch.object(app, "DB", self.db):
            result = app.get_comparison_data("pe_ttm")
        self.assertTrue(result["strict_comparison"])
        self.assertEqual(result["common_date"], rows[-1][0])
        self.assertEqual(len(result["series"]), 4)
        self.assertTrue(all(item["available"] for item in result["series"]))

    def test_missing_indices_are_not_replaced_with_demo_data(self):
        rows = monthly_rows()
        update_history("000300", "pb", db_path=self.db,
                       fetcher=lambda *_: fetched("000300", "pb", rows))
        with patch.object(app, "DB", self.db):
            result = app.get_comparison_data("pb")
        self.assertFalse(result["strict_comparison"])
        self.assertIsNone(result["common_date"])
        available = [item["index"] for item in result["series"] if item["available"]]
        self.assertEqual(available, ["000300"])
        self.assertTrue(all(not item["rows"] for item in result["series"] if not item["available"]))
        self.assertIn("未使用演示数据", result["warnings"][0])

    def test_mixed_observation_frequencies_reject_strict_ranking(self):
        rows = monthly_rows()
        start = date(2026, 1, 1)
        end = date(2026, 10, 31)
        daily_rows = []
        cursor = start
        while cursor <= end:
            daily_rows.append((cursor.isoformat(), 18.0 + len(daily_rows) * .001))
            cursor += timedelta(days=1)

        for code in ("000016", "000300", "000905", "000852"):
            item_rows = daily_rows if code == "000852" else rows
            update_history(
                code, "pe_ttm", db_path=self.db,
                fetcher=lambda *_args, c=code, r=item_rows: fetched(c, "pe_ttm", r),
            )
        with patch.object(app, "DB", self.db):
            result = app.get_comparison_data("pe_ttm")
        self.assertTrue(result["source_consistent"])
        self.assertFalse(result["frequency_consistent"])
        self.assertFalse(result["strict_comparison"])
        self.assertIsNotNone(result["common_date"])
        self.assertTrue(any("采样频率" in warning for warning in result["warnings"]))

    def test_different_sources_disable_strict_ranking(self):
        rows = monthly_rows()
        for code in ("000016", "000300", "000905", "000852"):
            update_history(code, "pe_ttm", db_path=self.db,
                           fetcher=lambda *_args, c=code: fetched(c, "pe_ttm", rows))
        import_history("000852", "pe_ttm", rows, "用户自定义口径", db_path=self.db)
        with patch.object(app, "DB", self.db):
            result = app.get_comparison_data("pe_ttm")
        self.assertEqual(result["common_date"], rows[-1][0])
        self.assertFalse(result["source_consistent"])
        self.assertFalse(result["strict_comparison"])
        self.assertTrue(any("不同数据来源或估值口径" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
