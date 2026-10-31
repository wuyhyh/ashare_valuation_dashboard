"""Offline tests for runtime transparency and explicit data synchronisation."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import TestCase
from unittest.mock import patch

import app


class RuntimeInfoTests(TestCase):
    def test_runtime_reports_actual_db_not_codex_reported_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "my-local.sqlite3"
            with patch.object(app, "DB", db):
                info = app.get_runtime_info()
                self.assertFalse(info["database_exists"])
                self.assertEqual(info["coverage"], {"available": 0, "total": 12})
                self.assertEqual(info["database_path"], str(db.resolve()))
                self.assertIn("python app.py sync", info["note"])

                sqlite3.connect(db).close()
                info = app.get_runtime_info()
                self.assertTrue(info["database_exists"])
                self.assertEqual(info["coverage"]["available"], 0)

    def test_empty_database_never_returns_automatic_demo(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "local.sqlite3"
            with patch.object(app, "DB", db):
                server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host = f"http://127.0.0.1:{server.server_port}"
                    with urllib.request.urlopen(host + "/api/history?index=000300&metric=pe_ttm") as reply:
                        import json
                        payload = json.load(reply)
                    self.assertFalse(payload["is_demo"])
                    self.assertEqual(payload["rows"], [])

                    with urllib.request.urlopen(host + "/api/history?index=000300&metric=pe_ttm&demo=1") as reply:
                        import json
                        explicit_demo = json.load(reply)
                    self.assertTrue(explicit_demo["is_demo"])
                    self.assertGreater(len(explicit_demo["rows"]), 20)

                    with urllib.request.urlopen(host + "/api/runtime") as reply:
                        import json
                        info = json.load(reply)
                    self.assertEqual(info["database_path"], str(db.resolve()))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_sync_makes_backup_and_runs_twelve_explicit_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "my-local.sqlite3"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE user_data (value TEXT)")
                con.execute("INSERT INTO user_data VALUES ('keep-me')")
            market_calls, valuation_calls = [], []

            def market(index_code, start_date, provider_name):
                market_calls.append((index_code, start_date, provider_name))
                return {"last_date": "2026-10-30", "count": 120}

            def valuation(index_code, metric):
                valuation_calls.append((index_code, metric))
                return {"last_date": "2026-10-30", "count": 120}

            with patch.object(app, "DB", db), patch.object(app, "update_market", side_effect=market), patch.object(app, "update_akshare", side_effect=valuation), patch.object(app.time, "sleep"):
                result = app.sync_all_real_data(pause_seconds=0)
                self.assertEqual(result["success"], 12)
                self.assertEqual(result["failed"], 0)
                self.assertEqual(result["skipped"], 0)
                self.assertEqual(len(market_calls), 4)
                self.assertEqual(len(valuation_calls), 8)
                self.assertTrue(all(call[2] == "csindex_official" for call in market_calls))
                self.assertTrue(Path(result["backup"]).exists())
                with sqlite3.connect(result["backup"]) as conn:
                    self.assertEqual(conn.execute("SELECT value FROM user_data").fetchone()[0], "keep-me")

    def test_sync_failure_keeps_attempting_other_indices(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "empty.sqlite3"
            def maybe_fail(code, metric):
                if code == "000905" and metric == "pb":
                    raise RuntimeError("simulated 429")
                return {"count": 120, "last_date": "2026-10-30"}
            with patch.object(app, "DB", db), patch.object(app, "update_market", return_value={"count": 200, "last_date": "2026-10-30"}), patch.object(app, "update_akshare", side_effect=maybe_fail), patch.object(app.time, "sleep"):
                result = app.sync_all_real_data(pause_seconds=0)
            self.assertEqual(result["success"], 11)
            self.assertEqual(result["failed"], 1)
            self.assertIn("simulated 429", result["results"][-2]["error"] if result["results"][-2]["status"] == "failed" else str(result["results"]))


if __name__ == "__main__":
    import unittest
    unittest.main()
