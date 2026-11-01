"""Offline contracts: separate index points from raw gold ETF price."""
from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
from threading import Thread
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import urlopen

import pytest
import app
import asset_watch


def records(asset_id, *, count=70, last=date(2026,10,30), first=None):
    # Unique weekdays are enough to test ordering; not claimed as exchange sessions.
    dates=[]
    d=last
    while len(dates)<count:
        if d.weekday()<5:
            dates.append(d)
        d-=timedelta(days=1)
    dates.sort()
    is_etf=asset_id=="518880"
    return {
        "ok":True,"version":"offline-test",
        "columns":["日期","收盘"] if is_etf else ["date","close"],
        "date_col":"日期" if is_etf else "date",
        "close_col":"收盘" if is_etf else "close",
        "records":[{
            ("日期" if is_etf else "date"):day.isoformat(),
            ("收盘" if is_etf else "close"):round(5+i*.03,3)
        }for i,day in enumerate(dates)]
    }


def test_asset_codes_source_types_and_distinct_price_units():
    assert asset_watch.ASSETS["000688"]["symbol"]=="sh000688"
    assert asset_watch.ASSETS["399673"]["symbol"]=="sz399673"
    assert asset_watch.ASSETS["518880"]["symbol"]=="518880"
    assert asset_watch.ASSETS["518880"]["unit"]=="元/份"
    assert asset_watch.ASSETS["000688"]["unit"]=="点"
    assert "净值" in asset_watch.ASSETS["518880"]["method"]
    assert len({a["source_interface"] for a in asset_watch.ASSETS.values()})==2


@pytest.mark.parametrize("code",list(asset_watch.ASSETS))
def test_each_data_source_normalizes_real_columns(code):
    result=asset_watch.fetch_history(code,fetcher=lambda _id: records(_id))
    assert result["asset_id"]==code
    assert len(result["rows"])==70
    assert result["last_date"]=="2026-10-30"
    assert result["first_date"] < result["last_date"]
    assert all(val>0 for _,val in result["rows"])


@pytest.mark.parametrize("code",list(asset_watch.ASSETS))
def test_missing_columns_bad_prices_future_dates_or_duplicates_rejected(code):
    base=records(code)
    bad=dict(base,columns=["错误字段"])
    with pytest.raises(asset_watch.AssetDataError,match="字段"):
        asset_watch.fetch_history(code,fetcher=lambda _:bad)
    dup=dict(base,records=[*base["records"],base["records"][-1]])
    with pytest.raises(asset_watch.AssetDataError,match="重复日期"):
        asset_watch.fetch_history(code,fetcher=lambda _:dup)
    invalid=dict(base,records=[*base["records"][:-1],{
        base["date_col"]:base["records"][-1][base["date_col"]],
        base["close_col"]:"not a price"
    }])
    with pytest.raises(asset_watch.AssetDataError,match="无法识别"):
        asset_watch.fetch_history(code,fetcher=lambda _:invalid)
    beyond=dict(base,records=[*base["records"],{
        base["date_col"]:"2099-01-01",base["close_col"]:10
    }])
    with pytest.raises(asset_watch.AssetDataError,match="未来日期"):
        asset_watch.fetch_history(code,fetcher=lambda _:beyond)


def test_old_long_history_cannot_be_replaced_by_recent_short_subset():
    with tempfile.TemporaryDirectory() as directory:
        db=Path(directory)/"asset_prices.sqlite3"
        asset_watch.update_history("399673",db_path=db,fetcher=lambda _:records("399673",count=250))
        before=asset_watch.report("399673",db)
        with pytest.raises(asset_watch.AssetDataError,match="历史比"):
            asset_watch.update_history("399673",db_path=db,fetcher=lambda _:records("399673",count=70))
        after=asset_watch.report("399673",db)
        assert len(after["rows"])==250
        assert after["source"]["first_date"]==before["source"]["first_date"]


def test_sqlite_source_metadata_and_idempotent_updates():
    with tempfile.TemporaryDirectory() as directory:
        db=Path(directory)/"assets.sqlite3"
        for code in asset_watch.ASSETS:
            asset_watch.update_history(code,db_path=db,fetcher=lambda name:records(name))
        first=asset_watch.overview(db)
        assert all(x["available"] for x in first["assets"])
        asset_watch.update_history("518880",db_path=db,fetcher=lambda name:records(name))
        report=asset_watch.report("518880",db)
        assert len(report["rows"])==70
        assert report["source"]["source_interface"]=="ak.fund_etf_hist_em"
        assert report["source"]["unit"]=="元/份"
        assert report["source"]["last_date"]=="2026-10-30"
        assert report["stats"]["3m"]["available"]
        assert not report["stats"]["10y"]["available"]
        assert "不含" in report["notice"]


def test_real_10_year_rule_cannot_fabricate_long_history():
    rows=[{"date":"2019-12-31","close":1000},{"date":"2026-10-30","close":1500}]
    result=asset_watch._statistics(rows,"10y")
    assert not result["available"]
    assert asset_watch._statistics(rows,"all")["available"]


def test_drawdown_and_price_return_are_arithmetic_and_not_pe():
    series=[
        {"date":"2016-10-28","close":100.0},
        {"date":"2020-10-30","close":150.0},
        {"date":"2026-10-30","close":120.0},
    ]
    summary=asset_watch._statistics(series,"10y")
    assert summary["available"]
    assert summary["return_pct"]==pytest.approx(20.0)
    assert summary["max_drawdown_pct"]==pytest.approx(-20.0)


def test_get_assets_api_returns_no_demo_for_uninitialized_db():
    with tempfile.TemporaryDirectory() as directory:
        db=Path(directory)/"no_asset_data.sqlite3"
        with patch.object(app,"ASSETS_DB",db):
            server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
            t=Thread(target=server.serve_forever,daemon=True)
            t.start()
            try:
                base=f"http://127.0.0.1:{server.server_port}"
                with urlopen(base+"/api/assets?asset=518880") as resp:
                    obj=json.load(resp)
                assert not obj["available"]
                assert obj["rows"]==[]
                assert not db.exists()
                with urlopen(base+"/api/assets") as resp:
                    overview=json.load(resp)
                assert len(overview["assets"])==3
            finally:
                server.shutdown();server.server_close();t.join(timeout=3)


def test_price_quality_reports_stale_feeds_and_extreme_unadjusted_jumps():
    rows = [
        {"date": "2026-08-03", "close": 2.0},
        {"date": "2026-10-30", "close": 4.0},
    ]
    quality = asset_watch._price_quality(rows, asof=date(2026, 11, 15))
    assert quality["age_days"] == 16
    assert quality["max_gap_days"] > 20
    assert quality["large_daily_jumps"][0]["date"] == "2026-10-30"
    assert len(quality["warnings"]) == 3


def test_price_quality_warnings_do_not_mislabel_weekend_as_stale():
    rows = [{"date": "2026-10-29", "close": 100.0},
            {"date": "2026-10-30", "close": 101.0}]
    quality = asset_watch._price_quality(rows, asof=date(2026, 11, 1))
    assert quality["age_days"] == 2
    assert not quality["warnings"]


def test_price_quality_is_exposed_by_asset_report():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "assets.sqlite3"
        asset_watch.update_history("518880", db_path=db,
            fetcher=lambda name: records(name))
        snapshot = asset_watch.report("518880", db)
        assert "quality" in snapshot
        assert isinstance(snapshot["quality"]["warnings"], list)
        assert snapshot["quality"]["max_gap_days"] >= 1
