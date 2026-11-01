"""Playwright UI acceptance: market breadth view, using explicit mock quotes.

This proves frontend behavior only. It does not validate live AKShare data.
Run while `python app.py` serves the development branch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8765")
ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


def groups():
    return {
        "ALL_all": (6, 4, 1, 1, 2, 3, 1, 1),
        "ALL_ex_st": (6, 3, 1, 1, 1, 2, 0, 1),
        "SH_all": (3, 2, 1, 0, 1, 1, 0, 1),
        "SH_ex_st": (3, 1, 1, 0, 0, 1, 0, 1),
        "SZ_all": (3, 2, 0, 1, 1, 2, 1, 0),
        "SZ_ex_st": (3, 2, 0, 1, 1, 1, 0, 0),
    }


def fixture():
    data = {}
    for key, (identified, selected, up, down, st, traded, flat, missing) in groups().items():
        data[key] = {
            "identified_mainboard": identified, "selected_count": selected,
            "advances": up, "declines": down, "unchanged": flat,
            "risk_warning": st, "traded_count": traded, "valid_quote_count": traded,
            "zero_turnover_count": 0, "missing_quote_count": missing,
            "advance_ratio": up / traded if traded else None,
            "median_change_pct": 0.25, "turnover_weighted_change_pct": 0.42,
        }
    return {
        "available": True,
        "capture": {
            "captured_at_utc": "2026-11-01T02:00:00+00:00",
            "source_interface": "tests.mocked_snapshot",
            "source_version": "offline-fixture",
            "trading_date": None,
            "trading_date_verified": False,
            "quality": {"raw_count": 20},
        },
        "groups": data,
        "historical_trend_ready": False,
        "historical_trend_reason": "测试数据：无可信历史时间序列",
    }


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    errors = []
    state = {"empty": True}

    def intercept(route):
        payload = {"available": False, "message": "尚未采集真实主板宽度快照"} if state["empty"] else fixture()
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/breadth", intercept)
        page.goto(BASE_URL + "/breadth.html", wait_until="networkidle")
        assert page.locator("#breadth-empty").is_visible()
        assert "尚未采集" in page.locator("#breadth-empty").inner_text()
        assert not page.locator("#breadth-report").is_visible()
        state["empty"] = False
        page.locator("#breadth-reload").click()
        page.locator("#breadth-report").wait_for(state="visible")
        assert "未验证" in page.locator("#breadth-date-trust").inner_text()
        assert page.locator("#b-up").inner_text() == "1"
        assert page.locator("#b-traded").inner_text() == "3"

        page.locator("#breadth-exchange").select_option("SH")
        assert page.locator("#b-traded").inner_text() == "1"
        page.locator("#breadth-no-st").check()
        assert page.locator("#b-selected").inner_text() == "1"
        page.locator("#breadth-exchange").select_option("SZ")
        assert page.locator("#b-selected").inner_text() == "2"
        page.locator("#breadth-no-st").uncheck()
        assert page.locator("#b-selected").inner_text() == "2"
        page.screenshot(path=str(ARTIFACTS / "v09-breadth-desktop.png"))

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        assert page.locator("#breadth-exchange").is_visible()
        assert page.locator("#breadth-no-st").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 4"), "mobile horizontal overflow"
        page.screenshot(path=str(ARTIFACTS / "v09-breadth-mobile.png"))
        assert not errors, errors
        browser.close()
    print("v0.9 breadth Playwright UI tests passed (MOCK data only)")


if __name__ == "__main__":
    main()
