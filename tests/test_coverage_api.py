from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from data_sources.history import import_history


def monthly_rows(count=142, base=12.0):
    rows = []
    for offset in range(count):
        year, month_zero = divmod(2015 * 12 + offset, 12)
        rows.append((date(year, month_zero + 1, 28).isoformat(), base + offset * .02))
    return rows


class CoverageApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "valuation.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_coverage_reports_only_persisted_real_series(self):
        import_history("000300", "pe_ttm", monthly_rows(), "test valuation", db_path=self.db)
        import_history("000300", "index_close", monthly_rows(base=3800), "test close", db_path=self.db)
        with patch.object(app, "DB", self.db):
            result = app.get_coverage_data()

        self.assertEqual(result["summary"], {"available": 2, "total": 12})
        self.assertEqual(len(result["indices"]), 4)
        hs300 = next(item for item in result["indices"] if item["index"] == "000300")
        self.assertEqual(hs300["metrics"]["pe_ttm"]["count"], 142)
        self.assertEqual(hs300["metrics"]["index_close"]["count"], 142)
        self.assertIsNone(hs300["metrics"]["pe_ttm"]["missing_reason"])
        self.assertEqual(hs300["metrics"]["pb"]["count"], 0)
        self.assertEqual(hs300["metrics"]["pb"]["missing_reason"], "尚未采集真实数据")
        self.assertTrue(all(
            item["metrics"][metric]["data_state"] != "demo"
            for item in result["indices"]
            for metric in ("pe_ttm", "pb", "index_close")
        ))


if __name__ == "__main__":
    unittest.main()
