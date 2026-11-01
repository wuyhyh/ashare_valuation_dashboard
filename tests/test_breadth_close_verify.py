"""TEST FIXTURE ONLY — no real AKShare requests are performed here."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile

import pytest

import breadth
from breadth_close_verify import CloseVerificationError, verify_close
from breadth_history import breadth_trend


BOARDS = (
    ("600", "SH_MAIN", "SH"), ("000", "SZ_MAIN", "SZ"),
    ("300", "CHINEXT", "SZ"), ("688", "STAR", "SH"),
)


def make_capture(day="2026-10-28", *, hour=16, provider="tencent"):
    pool = {}
    rows = []
    for prefix, board, exchange in BOARDS:
        for n in range(7):
            code = prefix + str(n).zfill(3)
            pool[code] = {"board": board, "exchange": exchange,
                          "security_type": "A_SHARE", "name": "模拟证券" + code}
            rows.append({"代码": code, "名称": "模拟证券" + code, "最新价": 10.0+n*.5,
                         "涨跌幅": -1.0 if n%2 else 1.0, "成交额": 100000.0})
    return breadth.create_capture(
        rows, official_pool=pool, min_identified=1, source_interface="ak.stock_zh_a_spot_tx",
        source_name="TEST FIXTURE ONLY", captured_at=datetime.fromisoformat(
            day + f"T{hour:02d}:05:00+08:00"
        ), extra_quality={"provider": provider},
    )


def fake_daily(codes, day, provider):
    return {"ok": True, "akshare_version": "TEST FIXTURE",
            "rows": {code: [{"date": day, "close": 10+int(code[-3:])*.5}]
                     for code, _ in codes}}


def test_dry_run_does_not_promote_or_mutate_and_success_creates_backup():
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "capture.sqlite3"
        candidate = make_capture()
        breadth.persist_capture(candidate, db)
        original = db.read_bytes()
        result = verify_close(db, capture_id=candidate["capture_id"],
                              fetcher=fake_daily, dry_run=True)
        assert result["matched_samples"] == 20
        assert result["dry_run"] is True and result["written"] is False
        assert db.read_bytes() == original
        assert not list(Path(folder).glob("*.backup-before-close-verification-*"))
        promoted = verify_close(db, capture_id=candidate["capture_id"], fetcher=fake_daily)
        assert promoted["written"] is True
        backup = Path(promoted["backup"])
        assert backup.is_file() and backup.stat().st_size > 0
        with sqlite3.connect(backup) as connection:
            assert connection.execute("SELECT trading_date_verified FROM breadth_captures").fetchone()[0] == 0


def test_independently_checked_close_can_promote_only_hs_four_boards():
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "capture.sqlite3"
        for day in ("2026-10-28", "2026-10-29"):
            capture = make_capture(day)
            breadth.persist_capture(capture, db)
            status = verify_close(db, capture_id=capture["capture_id"], fetcher=fake_daily)
            assert status["matched_samples"] == 20
            assert status["trading_date"] == day
        report = breadth_trend(db, scope="SH_SZ_ALL")
        assert report["available"]
        assert report["point_count"] == 2
        assert report["observations"][0]["trading_date"] == "2026-10-28"
        assert breadth_trend(db, scope="BSE")["available"] is False
        with sqlite3.connect(db) as connection:
            rows = connection.execute(
                "SELECT trading_date, trading_date_verified, quality_json "
                "FROM breadth_captures ORDER BY trading_date"
            ).fetchall()
            assert all(row[1] == 1 for row in rows)
            assert all(json.loads(row[2])["close_verification"]["matched_samples"] == 20
                       for row in rows)


@pytest.mark.parametrize("alter", ["stale_close", "wrong_date", "incomplete", "bad_price"])
def test_unreliable_reference_is_rejected_without_database_promotion(alter):
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "capture.sqlite3"
        breadth.persist_capture(make_capture(), db)
        def invalid(codes, day, provider):
            info = fake_daily(codes, day, provider)
            first = codes[0][0]
            if alter == "stale_close":
                info["rows"][first][0]["close"] = 20.0
            elif alter == "wrong_date":
                info["rows"][first][0]["date"] = "2026-10-27"
            elif alter == "incomplete":
                info["rows"][first] = []
            else:
                info["rows"][first][0]["close"] = 0
            return info
        with pytest.raises(CloseVerificationError):
            verify_close(db, fetcher=invalid)
        with sqlite3.connect(db) as connection:
            flag = connection.execute(
                "SELECT trading_date_verified FROM breadth_captures"
            ).fetchone()[0]
            assert flag == 0
        assert not breadth_trend(db)["available"]


def test_weekend_and_intraday_snapshot_rejected_before_reference_requests():
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "capture.sqlite3"
        calls = []
        def must_not_fetch(*args):
            calls.append(args)
            raise AssertionError("called remote provider when time not eligible")
        for day, hour in (("2026-11-01", 17), ("2026-10-28", 14)):
            capture = make_capture(day, hour=hour)
            breadth.persist_capture(capture, db)
            with pytest.raises(CloseVerificationError):
                verify_close(db, capture_id=capture["capture_id"], fetcher=must_not_fetch)
        assert not calls


def test_same_provider_cannot_pass_as_independent_and_source_fail_closed():
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "capture.sqlite3"
        breadth.persist_capture(make_capture(provider="sina"), db)
        with pytest.raises(CloseVerificationError, match="不同"):
            verify_close(db, fetcher=fake_daily, reference_provider="sina")
        assert not breadth_trend(db)["available"]
