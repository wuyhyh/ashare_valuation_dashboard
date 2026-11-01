"""TEST FIXTURE ONLY — validates strict-date linkage without market claims."""
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

import app
import breadth_linkage as module
from data_sources.history import import_history


def mocked_trend(days, values=None):
    values = values or [0.2 + index * 0.01 for index in range(len(days))]
    return {
        "available": len(days) >= 2,
        "point_count": len(days),
        "observations": [
            {"trading_date": day, "advance_ratio": value}
            for day, value in zip(days, values)
        ],
    }


def weekdays(count, start=date(2026, 9, 1)):
    result = []
    cursor = start
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def test_linkage_remains_unavailable_without_verified_history(tmp_path):
    result = module.breadth_linkage(
        tmp_path / "missing-breadth.sqlite3", tmp_path / "missing-valuation.sqlite3"
    )
    assert result["available"] is False
    assert result["indices"] == {}
    assert result["matching_rule"] == "exact_same_date_no_fill_no_interpolation"


def test_dense_exact_dates_enable_exploratory_divergence_only(monkeypatch):
    with tempfile.TemporaryDirectory() as folder:
        valuation_db = Path(folder) / "valuation.sqlite3"
        days = weekdays(20)
        import_history(
            "000300", "index_close",
            [(day, 4000 + index * 10) for index, day in enumerate(days)],
            "TEST FIXTURE ONLY", db_path=valuation_db,
        )
        monkeypatch.setattr(module, "breadth_trend", lambda *_a, **_k: mocked_trend(days))
        before = valuation_db.read_bytes()
        result = module.breadth_linkage(Path(folder) / "ignored.sqlite3", valuation_db)
        item = result["indices"]["000300"]["divergence"]
        assert item["available"] is True
        assert item["start_date"] == days[0] and item["end_date"] == days[-1]
        assert item["breadth_change_percentage_points"] == pytest.approx(19)
        assert result["indices"]["000300"]["valuation_relationship"]["pe_ttm"]["available"] is False
        assert valuation_db.read_bytes() == before


def test_monthly_valuation_uses_only_exact_common_dates_without_fill(monkeypatch):
    with tempfile.TemporaryDirectory() as folder:
        valuation_db = Path(folder) / "valuation.sqlite3"
        days = [date(2025 + offset // 12, offset % 12 + 1, 15).isoformat() for offset in range(12)]
        breadth_values = [0.3 + index * 0.02 for index in range(12)]
        import_history(
            "000300", "pe_ttm",
            [(day, 10 + index * 0.2) for index, day in enumerate(days)],
            "TEST FIXTURE ONLY", db_path=valuation_db,
        )
        import_history(
            "000905", "pe_ttm",
            [((date.fromisoformat(day) + timedelta(days=1)).isoformat(), 20 + index)
             for index, day in enumerate(days)],
            "TEST FIXTURE ONLY", db_path=valuation_db,
        )
        monkeypatch.setattr(
            module, "breadth_trend",
            lambda *_a, **_k: mocked_trend(days, breadth_values),
        )
        result = module.breadth_linkage(Path(folder) / "ignored.sqlite3", valuation_db)
        hs300 = result["indices"]["000300"]["valuation_relationship"]["pe_ttm"]
        csi500 = result["indices"]["000905"]["valuation_relationship"]["pe_ttm"]
        assert hs300["available"] is True and hs300["common_dates"] == 12
        assert hs300["frequency"] == "monthly"
        assert hs300["correlation"] == pytest.approx(1.0)
        assert csi500["available"] is False and csi500["common_dates"] == 0


def test_weekly_index_points_cannot_masquerade_as_daily_divergence(monkeypatch):
    with tempfile.TemporaryDirectory() as folder:
        valuation_db = Path(folder) / "valuation.sqlite3"
        days = [(date(2026, 1, 2) + timedelta(days=7 * index)).isoformat()
                for index in range(20)]
        import_history(
            "000300", "index_close",
            [(day, 4000 + index) for index, day in enumerate(days)],
            "TEST FIXTURE ONLY", db_path=valuation_db,
        )
        monkeypatch.setattr(module, "breadth_trend", lambda *_a, **_k: mocked_trend(days))
        item = module.breadth_linkage(
            Path(folder) / "ignored.sqlite3", valuation_db,
        )["indices"]["000300"]
        assert item["index_close_frequency"] == "weekly"
        assert item["divergence"]["available"] is False
        assert "不是已识别的真实日频" in item["divergence"]["message"]


def test_linkage_http_endpoint_validates_query_and_returns_gate_state():
    fixture = {"available": False, "message": "TEST FIXTURE ONLY", "indices": {}}
    with patch.object(app, "breadth_linkage", return_value=fixture) as linked:
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/breadth-linkage"
            with urllib.request.urlopen(url + "?scope=SH_SZ_ALL&exclude_st=1&limit=180") as reply:
                assert json.load(reply) == fixture
            linked.assert_called_once_with(
                app.BREADTH_DB, app.DB, scope="SH_SZ_ALL", exclude_st=True, limit=180,
            )
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url + "?exclude_st=yes")
            assert caught.value.code == 400
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
