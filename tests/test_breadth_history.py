"""v0.11 evidence-gated history tests; every 'verified' sample here is TEST FIXTURE.

The tests validate only the contract, not live market-data verification.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pytest

import app
import breadth
from breadth_history import breadth_trend, EVIDENCE_METHOD, HISTORY_CALCULATION_VERSION


def rows():
    result = []
    for prefix in ("600", "000", "300", "688"):
        for index in range(5):
            code = prefix + str(index).zfill(3)
            result.append({
                "代码": code, "名称": "模拟股票" + code, "最新价": 10.0,
                "涨跌幅": 1.0 if index % 2 else -1.0, "成交额": 100000.0,
            })
    return result


def official_pool():
    boards = (("600", "SH_MAIN", "SH"), ("000", "SZ_MAIN", "SZ"),
              ("300", "CHINEXT", "SZ"), ("688", "STAR", "SH"))
    return {
        prefix + str(index).zfill(3): {
            "name": "模拟股票" + prefix + str(index).zfill(3),
            "board": board, "exchange": exchange, "security_type": "A_SHARE",
        }
        for prefix, board, exchange in boards for index in range(5)
    }


def capture(day, *, hour=16, source="source-TX", verified=True, evidence=True,
            session_day=None, ratio=1.0, marker="passed"):
    timestamp = datetime.fromisoformat(day + f"T{hour:02d}:10:00+08:00")
    result = breadth.create_capture(
        rows(), captured_at=timestamp, source_name="TEST FIXTURE ONLY",
        source_interface=source, min_identified=1, official_pool=official_pool(),
    )
    if verified:
        result["trading_date"] = day
        result["trading_date_verified"] = True
    if evidence:
        result["quality"]["close_verification"] = {
            "status": marker, "method": EVIDENCE_METHOD,
            "calculation_version": HISTORY_CALCULATION_VERSION,
            "reference_source": "TEST FIXTURE ONLY - independent stock daily close",
            "reference_trading_date": session_day or day,
            "checked_samples": 20, "matched_samples": round(20 * ratio),
            "max_relative_diff": 0.0001,
            "checked_boards": ["SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"],
        }
    return result


def test_unverified_snapshots_cannot_be_a_trend_even_when_captured_after_close():
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "breadth.sqlite3"
        breadth.persist_capture(capture("2026-10-28", verified=False), db)
        breadth.persist_capture(capture("2026-10-29", evidence=False), db)
        result = breadth_trend(db)
        assert result["available"] is False
        assert result["observations"] == []
        assert result["point_count"] == 0


def test_independent_evidence_and_same_source_are_required_with_deduplication():
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "breadth.sqlite3"
        older = capture("2026-10-28")
        newer = capture("2026-10-28", hour=17)
        newer["quotes"][0]["change_pct"] = 2.0
        breadth.persist_capture(older, db)
        breadth.persist_capture(newer, db)
        breadth.persist_capture(capture("2026-10-29"), db)
        breadth.persist_capture(capture("2026-10-30", source="other-provider"), db)
        result = breadth_trend(db, scope="SH_SZ_ALL")
        assert result["available"] is False
        assert result["point_count"] == 1
        # Newest verified cohort is other-provider, no cross-provider stitching.
        breadth.persist_capture(capture("2026-10-30", hour=17, source="source-TX"), db)
        result = breadth_trend(db, scope="SH_SZ_ALL")
        assert result["available"] is True
        assert result["point_count"] == 3
        assert [p["trading_date"] for p in result["observations"]] == [
            "2026-10-28", "2026-10-29", "2026-10-30",
        ]
        assert result["source_interface"] == "source-TX"
        assert result["observations"][0]["advances"] == 9
        assert result["observations"][0]["traded_count"] == 20


@pytest.mark.parametrize("change", [
    {"hour": 14}, {"session_day": "2026-10-29"}, {"ratio": 0.9},
    {"marker": "failed"},
])
def test_insufficient_close_verification_must_not_promote_fake_history(change):
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "breadth.sqlite3"
        breadth.persist_capture(capture("2026-10-28", **change), db)
        breadth.persist_capture(capture("2026-10-29"), db)
        result = breadth_trend(db)
        assert result["available"] is False


def test_invalid_scope_and_limit_are_rejected_and_legacy_cannot_fake_full_market():
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "breadth.sqlite3"
        with pytest.raises(ValueError, match="范围"):
            breadth_trend(db, scope="NOPE")
        with pytest.raises(ValueError, match="180"):
            breadth_trend(db, limit=1000)
        old = breadth.create_capture(
            rows(), captured_at=datetime(2026,10,28,8,10,tzinfo=timezone.utc),
            source_name="TEST FIXTURE ONLY", source_interface="source-TX", min_identified=1,
        )
        old["trading_date"] = "2026-10-28"
        old["trading_date_verified"] = True
        old["quality"]["close_verification"] = capture("2026-10-28")["quality"]["close_verification"]
        breadth.persist_capture(old, db)
        assert breadth_trend(db, scope="SH_SZ_ALL")["point_count"] == 0
        assert breadth_trend(db, scope="CHINEXT")["point_count"] == 0


def test_history_endpoint_is_read_only_and_rejects_invalid_query():
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "breadth.sqlite3"
        breadth.persist_capture(capture("2026-10-28"), db)
        breadth.persist_capture(capture("2026-10-29"), db)
        before = db.stat().st_size
        with patch.object(app, "BREADTH_DB", db):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/breadth-trend"
                with urllib.request.urlopen(url + "?scope=SH_SZ_ALL&exclude_st=0") as reply:
                    payload = json.load(reply)
                assert payload["available"] and len(payload["observations"]) == 2
                assert payload["observations"][0]["trading_date"] == "2026-10-28"
                for suffix in ("?scope=UNKNOWN", "?exclude_st=yes", "?limit=10000"):
                    with pytest.raises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(url + suffix)
                    assert caught.value.code == 400
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)
        assert db.stat().st_size == before


def test_verified_session_moving_averages_require_enough_dense_real_dates():
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "breadth.sqlite3"
        days = []
        cursor = date(2026, 9, 21)
        while len(days) < 20:
            if cursor.weekday() < 5:
                days.append(cursor.isoformat())
            cursor += timedelta(days=1)
        for day in days:
            breadth.persist_capture(capture(day), db)
        result = breadth_trend(db, limit=30)
        assert result["available"] is True and result["point_count"] == 20
        assert result["observations"][3]["advance_ratio_ma5"] is None
        assert result["observations"][4]["advance_ratio_ma5"] == pytest.approx(.4)
        assert result["observations"][18]["advance_ratio_ma20"] is None
        assert result["observations"][19]["advance_ratio_ma20"] == pytest.approx(.4)
        assert result["moving_averages"]["ma5_ready"] is True
        assert result["moving_averages"]["ma20_ready"] is True
