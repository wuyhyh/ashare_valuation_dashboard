"""TEST FIXTURE ONLY — validates strict-date linkage without market claims."""
from datetime import date, datetime, timedelta
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
import breadth
import breadth_linkage as module
from breadth_history import EVIDENCE_METHOD, HISTORY_CALCULATION_VERSION, breadth_trend
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
        "source_interface": "TEST FIXTURE ONLY",
        "coverage_scope": "sh_sz_all_a",
    }


def weekdays(count, start=date(2026, 9, 1)):
    result = []
    cursor = start
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def verified_capture(day, *, source="TEST-FIXTURE-A", advance_count=10):
    boards = (("600", "SH_MAIN", "SH"), ("000", "SZ_MAIN", "SZ"),
              ("300", "CHINEXT", "SZ"), ("688", "STAR", "SH"))
    rows = []
    pool = {}
    position = 0
    for prefix, board, exchange in boards:
        for index in range(5):
            code = prefix + str(index).zfill(3)
            name = "TEST FIXTURE ONLY " + code
            rows.append({
                "代码": code, "名称": name, "最新价": 10 + index,
                "涨跌幅": 1.0 if position < advance_count else -1.0,
                "成交额": 100000.0,
            })
            pool[code] = {
                "name": name, "board": board, "exchange": exchange,
                "security_type": "A_SHARE",
            }
            position += 1
    result = breadth.create_capture(
        rows, captured_at=datetime.fromisoformat(day + "T16:10:00+08:00"),
        source_name="TEST FIXTURE ONLY", source_interface=source,
        min_identified=1, official_pool=pool,
    )
    result["trading_date"] = day
    result["trading_date_verified"] = True
    result["quality"]["close_verification"] = {
        "status": "passed", "method": EVIDENCE_METHOD,
        "calculation_version": HISTORY_CALCULATION_VERSION,
        "reference_source": "TEST FIXTURE ONLY independent close",
        "reference_trading_date": day, "checked_samples": 20,
        "matched_samples": 20, "max_relative_diff": .0001,
        "checked_boards": ["SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"],
    }
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
        monkeypatch.setattr(module, "breadth_linkage_history", lambda *_a, **_k: mocked_trend(days))
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
            module, "breadth_linkage_history",
            lambda *_a, **_k: mocked_trend(days, breadth_values),
        )
        result = module.breadth_linkage(Path(folder) / "ignored.sqlite3", valuation_db)
        hs300 = result["indices"]["000300"]["valuation_relationship"]["pe_ttm"]
        csi500 = result["indices"]["000905"]["valuation_relationship"]["pe_ttm"]
        assert hs300["available"] is True and hs300["common_dates"] == 12
        assert hs300["common_months"] == 12 and hs300["missing_month_count"] == 0
        assert hs300["frequency"] == "monthly"
        assert hs300["correlation"] == pytest.approx(1.0)
        assert csi500["available"] is False and csi500["common_dates"] == 0


def test_long_history_supports_twelve_complete_months_beyond_chart_limit():
    with tempfile.TemporaryDirectory() as folder:
        breadth_db = Path(folder) / "breadth.sqlite3"
        valuation_db = Path(folder) / "valuation.sqlite3"
        cursor = date(2025, 1, 2)
        end = date(2026, 1, 30)
        days = []
        while cursor <= end:
            if cursor.weekday() < 5:
                days.append(cursor.isoformat())
            cursor += timedelta(days=1)
        for index, day in enumerate(days):
            breadth.persist_capture(
                verified_capture(day, advance_count=5 + index % 11), breadth_db,
            )
        month_ends = {}
        for day in days:
            month_ends[day[:7]] = day
        valuation_days = [month_ends[month] for month in sorted(month_ends)[1:13]]
        import_history(
            "000300", "index_close",
            [(day, 4000 + index) for index, day in enumerate(days)],
            "TEST FIXTURE ONLY", db_path=valuation_db,
        )
        import_history(
            "000300", "pe_ttm",
            [(day, 11 + index * .1) for index, day in enumerate(valuation_days)],
            "TEST FIXTURE ONLY", db_path=valuation_db,
        )
        result = module.breadth_linkage(breadth_db, valuation_db)
        pe = result["indices"]["000300"]["valuation_relationship"]["pe_ttm"]
        chart = breadth_trend(breadth_db, limit=180)
        assert chart["point_count"] == 180
        assert result["breadth_history_query_limit"] == 420
        assert result["breadth_points"] > 180
        assert pe["available"] is True
        assert pe["common_months"] == 12
        assert pe["calendar_month_span"] == 12
        assert pe["missing_month_count"] == 0


def test_missing_months_and_mixed_breadth_sources_do_not_form_a_series():
    with tempfile.TemporaryDirectory() as folder:
        breadth_db = Path(folder) / "breadth.sqlite3"
        valuation_db = Path(folder) / "valuation.sqlite3"
        dates = []
        for offset in range(12):
            candidate = date(2025 + offset // 12, offset % 12 + 1, 15)
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
            dates.append(candidate.isoformat())
        for index, day in enumerate(dates):
            source = "TEST-FIXTURE-A" if index < 6 else "TEST-FIXTURE-B"
            breadth.persist_capture(verified_capture(day, source=source, advance_count=5+index), breadth_db)
        import_history(
            "000300", "pe_ttm", [(day, 10 + index) for index, day in enumerate(dates)],
            "TEST FIXTURE ONLY", db_path=valuation_db,
        )
        result = module.breadth_linkage(breadth_db, valuation_db)
        pe = result["indices"]["000300"]["valuation_relationship"]["pe_ttm"]
        assert result["breadth_source_interface"] == "TEST-FIXTURE-B"
        assert pe["available"] is False and pe["common_months"] == 6

        missing_days = dates[:5] + dates[6:] + ["2026-01-15"]
        monkeypatch_result = mocked_trend(missing_days)
        with patch.object(module, "breadth_linkage_history", return_value=monkeypatch_result):
            missing_db = Path(folder) / "missing.sqlite3"
            import_history(
                "000300", "pe_ttm",
                [(day, 20 + index) for index, day in enumerate(missing_days)],
                "TEST FIXTURE ONLY", db_path=missing_db,
            )
            missing = module.breadth_linkage(Path(folder) / "ignored.sqlite3", missing_db)
        missing_pe = missing["indices"]["000300"]["valuation_relationship"]["pe_ttm"]
        assert missing_pe["available"] is False
        assert missing_pe["missing_month_count"] == 1
        assert missing_pe["max_missing_consecutive_months"] == 1
        assert "缺失月份" in missing_pe["message"]


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
        monkeypatch.setattr(module, "breadth_linkage_history", lambda *_a, **_k: mocked_trend(days))
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
            with urllib.request.urlopen(url + "?scope=SH_SZ_ALL&exclude_st=1") as reply:
                assert json.load(reply) == fixture
            linked.assert_called_once_with(
                app.BREADTH_DB, app.DB, scope="SH_SZ_ALL", exclude_st=True,
            )
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url + "?exclude_st=yes")
            assert caught.value.code == 400
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
