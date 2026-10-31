"""Offline contract tests for official CSI opt-in source; never asserts live availability."""
from datetime import date, timedelta
from pathlib import Path
import sqlite3

import pytest

from data_sources.csindex import diagnose_official, fetch_official, update_official
from data_sources.history import read_active_history


def response(code="000300", n=25, include_pe=True):
    first = date(2026, 9, 1)
    records = []
    for index in range(n):
        row = {"日期": (first + timedelta(days=index)).isoformat(),
               "指数代码": code, "收盘": 4000 + index}
        if include_pe:
            row["滚动市盈率"] = 12 + index * .01
        records.append(row)
    return {"columns": list(records[0]), "records": records, "version": "fixture"}


def test_valid_official_series_are_separate_from_existing_source(tmp_path):
    db = tmp_path / "valuation.sqlite3"
    provider = lambda code, start, end, timeout: response(code)
    result = update_official("000300", db_path=db, provider=provider)
    assert set(result) == {"pe_ttm", "index_close"}
    close, close_status = read_active_history(db, "000300", "index_close")
    pe, pe_status = read_active_history(db, "000300", "pe_ttm")
    assert len(close) == len(pe) == 25
    assert close_status["series_id"] != pe_status["series_id"]
    assert "csindex_official" in pe_status["series_id"]
    assert update_official("000300", db_path=db, provider=provider)["pe_ttm"]["inserted"] == 0


def test_wrong_code_never_writes(tmp_path):
    db = tmp_path / "valuation.sqlite3"
    provider = lambda code, start, end, timeout: response("000905")
    with pytest.raises(Exception, match="不匹配"):
        update_official("000300", db_path=db, provider=provider)
    rows, _ = read_active_history(db, "000300", "index_close")
    assert not rows


def test_missing_pe_never_partially_writes(tmp_path):
    db = tmp_path / "valuation.sqlite3"
    provider = lambda code, start, end, timeout: response(code, include_pe=False)
    with pytest.raises(Exception, match="滚动市盈率"):
        update_official("000300", db_path=db, provider=provider)
    rows, _ = read_active_history(db, "000300", "index_close")
    assert not rows
    # A single-metric close import is allowed if explicitly selected.
    result = update_official("000300", metric="index_close", db_path=db, provider=provider)
    assert result["index_close"]["count"] == 25


def test_diagnose_does_not_write(tmp_path):
    result = diagnose_official("000300", provider=lambda *args: response())
    assert result["metrics"]["pe_ttm"]["valid_count"] == 25


def test_empty_or_future_range_rejected():
    with pytest.raises(ValueError, match="未来日期"):
        fetch_official("000300", end_date="20990101", provider=lambda *args: response())
    with pytest.raises(ValueError, match="日期范围"):
        fetch_official("000300", start_date="20261030", end_date="20260101", provider=lambda *args: response())
