"""Playwright UI acceptance for v0.10 breadth with explicit mock data only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8765")
ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


def statistic(scope, identified, up, down, flat=0, risk=0, missing=0):
    traded = up + down + flat
    return {
        "scope": scope, "identified_count": identified, "identified_mainboard": identified,
        "selected_count": identified, "advances": up, "declines": down, "unchanged": flat,
        "risk_warning": risk, "traded_count": traded, "valid_quote_count": traded,
        "zero_turnover_count": identified - traded - missing, "missing_quote_count": missing,
        "advance_ratio": up / traded if traded else None, "median_change_pct": -0.2,
        "turnover_weighted_change_pct": -0.35,
    }


def fixture():
    base = {
        "SH_MAIN": statistic("SH_MAIN", 2, 1, 1),
        "SZ_MAIN": statistic("SZ_MAIN", 2, 0, 1, 1),
        "CHINEXT": statistic("CHINEXT", 2, 1, 1, risk=1),
        "STAR": statistic("STAR", 2, 0, 2),
        "MAIN_ALL": statistic("MAIN_ALL", 4, 1, 2, 1),
        "SH_SZ_ALL": statistic("SH_SZ_ALL", 8, 2, 5, 1, risk=1),
        "BSE": statistic("BSE", 1, 1, 0),
        "ALL_A": statistic("ALL_A", 9, 3, 5, 1, risk=1),
    }
    groups = {}
    for scope, item in base.items():
        groups[scope + "_all"] = item
        filtered = dict(item)
        filtered["selected_count"] = item["selected_count"] - item["risk_warning"]
        groups[scope + "_ex_st"] = filtered
    groups.update({"ALL_all": groups["MAIN_ALL_all"], "SH_all": groups["SH_MAIN_all"], "SZ_all": groups["SZ_MAIN_all"]})
    boards = {
        board: {"official_count": count, "matched_count": count, "coverage_ratio": 1,
                "missing_count": 0, "unexpected_count": 0}
        for board, count in {"SH_MAIN": 2, "SZ_MAIN": 2, "CHINEXT": 2, "STAR": 2, "BSE": 1}.items()
    }
    return {
        "available": True, "default_scope": "SH_SZ_ALL",
        "available_scopes": ["SH_SZ_ALL", "MAIN_ALL", "SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR", "BSE", "ALL_A"],
        "scope_warning": None, "groups": groups,
        "capture": {
            "captured_at_utc": "2026-11-01T02:00:00+00:00", "coverage_scope": "sh_sz_all_a_with_bse",
            "source_interface": "tests.mocked_snapshot", "source_version": "offline-fixture",
            "trading_date": None, "trading_date_verified": False,
            "quality": {"raw_count": 10, "official_pool": {"boards": boards, "special_security_count": 1}},
        },
        "historical_trend_ready": False,
        "historical_trend_reason": "测试数据：无可信历史时间序列",
    }


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    errors = []
    state = {"empty": True, "verified": False}

    def intercept(route):
        payload = {"available": False, "message": "尚未采集真实市场宽度快照"} if state["empty"] else fixture()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    def intercept_trend(route):
        url = route.request.url
        # TEST FIXTURE ONLY: this does not verify any real market close.
        if not state["verified"]:
            payload = {
                "available": False, "point_count": 0, "observations": [],
                "message": "测试：尚无至少两个经过独立核验的交易日",
            }
        else:
            payload = {
                "available": True, "point_count": 2,
                "observations": [
                    {"trading_date": "2026-10-28", "advance_ratio": .30, "traded_count": 5000},
                    {"trading_date": "2026-10-29", "advance_ratio": .60, "traded_count": 5000},
                ],
                "message": "TEST FIXTURE ONLY - 模拟有效证据，非真实历史宽度",
            }
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/breadth", intercept)
        page.route("**/api/breadth-trend?*", intercept_trend)
        page.goto(BASE_URL + "/breadth.html", wait_until="networkidle")
        assert page.locator("#breadth-empty").is_visible()
        state["empty"] = False
        page.locator("#breadth-reload").click()
        page.locator("#breadth-report").wait_for(state="visible")
        assert page.locator("#breadth-exchange").input_value() == "SH_SZ_ALL"
        assert page.locator("#b-identified").inner_text() == "8"
        assert "未验证" in page.locator("#breadth-date-trust").inner_text()
        assert page.locator("#breadth-trend-empty").is_visible()
        assert not page.locator("#breadth-trend-chart").is_visible()
        state["verified"] = True
        page.locator("#breadth-reload").click()
        page.locator("#breadth-trend-chart").wait_for(state="visible")
        assert page.locator("#breadth-trend-count").inner_text() == "有效交易日：2"
        assert page.locator("#breadth-trend-start").inner_text() == "2026-10-28"
        assert page.locator("#breadth-trend-svg path").count() == 1

        for scope, expected in (("MAIN_ALL", "4"), ("CHINEXT", "2"), ("STAR", "2"), ("BSE", "1"), ("ALL_A", "9")):
            page.locator("#breadth-exchange").select_option(scope)
            assert page.locator("#b-identified").inner_text() == expected
            assert page.locator("#b-coverage").inner_text() == "100.0%"
        page.locator("button[data-scope='CHINEXT']").click()
        assert page.locator("#breadth-exchange").input_value() == "CHINEXT"
        page.locator("#breadth-no-st").check()
        assert page.locator("#b-selected").inner_text() == "1"
        page.screenshot(path=str(ARTIFACTS / "v010-breadth-desktop.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        assert page.locator("#breadth-board-grid").is_visible()
        assert page.locator("button[data-scope='STAR']").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 4"), "mobile horizontal overflow"
        page.screenshot(path=str(ARTIFACTS / "v010-breadth-mobile.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("v0.10 all-A-share breadth Playwright UI tests passed (MOCK data only)")


if __name__ == "__main__":
    main()
