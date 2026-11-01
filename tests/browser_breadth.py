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
                "moving_averages": {"calendar_available": True, "ma5_ready": False, "ma20_ready": False},
            }
        else:
            payload = {
                "available": True, "point_count": 2,
                "source_interface": "tests.fixture.breadth",
                "coverage_scope": "sh_sz_all_a",
                "moving_averages": {"calendar_available": True, "ma5_ready": False, "ma20_ready": False},
                "observations": [
                    {"trading_date": "2026-10-28", "advance_ratio": .30, "traded_count": 5000},
                    {"trading_date": "2026-10-29", "advance_ratio": .60, "traded_count": 5000},
                ],
                "message": "TEST FIXTURE ONLY - 模拟有效证据，非真实历史宽度",
            }
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))

    def intercept_linkage(route):
        if not state["verified"]:
            payload = {
                "available": False, "breadth_source_interface": None,
                "message": "TEST FIXTURE ONLY：可信宽度历史样本不足",
                "indices": {"000300": {
                    "index_close_source": "tests.fixture.index_close", "index_close_frequency": "daily",
                    "divergence": {"available": False, "common_dates": 2, "required_common_dates": 20,
                                   "message": "至少需要20个稠密、严格同日样本"},
                    "valuation_relationship": {"pe_ttm": {
                        "available": False, "common_dates": 1, "required_common_dates": 12,
                        "common_months": 1, "calendar_month_span": 1, "missing_month_count": 0,
                        "frequency": "monthly", "source_interface": "tests.fixture.pe",
                        "message": "至少需要12个连续自然月的严格同日估值样本",
                    }},
                }},
            }
        else:
            payload = {
                "available": True, "breadth_source_interface": "tests.fixture.breadth",
                "message": "TEST FIXTURE ONLY：仅展示达到门槛的探索性结果",
                "indices": {"000300": {
                    "index_close_source": "tests.fixture.index_close", "index_close_frequency": "daily",
                    "divergence": {
                        "available": True, "common_dates": 20, "required_common_dates": 20,
                        "start_date": "2026-10-01", "end_date": "2026-10-28",
                        "breadth_change_percentage_points": 8.5, "index_return_pct": 5,
                        "divergence_percentage_points": 3.5,
                        "calculation": "上涨比例百分点变化 − 指数百分比收益；仅为探索性差值",
                    },
                    "valuation_relationship": {
                        "pe_ttm": {
                            "available": True, "common_dates": 12, "required_common_dates": 12,
                            "common_months": 12, "calendar_month_span": 12,
                            "missing_month_count": 0, "frequency": "monthly",
                            "source_interface": "tests.fixture.pe", "start_date": "2025-11-28",
                            "end_date": "2026-10-28", "correlation": .62,
                            "calculation": "Pearson相关系数：同日上涨比例与估值水平；不填充、不插值",
                        },
                        "pb": {
                            "available": False, "common_dates": 0, "required_common_dates": 12,
                            "common_months": 0, "calendar_month_span": 0,
                            "missing_month_count": 0, "frequency": None,
                            "source_interface": None, "message": "尚未采集PB真实同日样本",
                        },
                    },
                }},
            }
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/breadth", intercept)
        page.route("**/api/breadth-trend?*", intercept_trend)
        page.route("**/api/breadth-linkage?*", intercept_linkage)
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
        assert "尚无至少两个经过独立核验" in page.locator("#breadth-history").inner_text()
        assert "已核验" not in page.locator("#breadth-history").inner_text()
        assert "样本不足" in page.locator("#breadth-linkage-status").inner_text()
        assert "2 / 20" in page.locator("#breadth-linkage-results").inner_text()
        assert "至少需要12个连续自然月" in page.locator("#breadth-linkage-results").inner_text()
        state["verified"] = True
        page.locator("#breadth-reload").click()
        page.locator("#breadth-trend-chart").wait_for(state="visible")
        assert page.locator("#breadth-trend-count").inner_text() == "有效交易日：2"
        assert page.locator("#breadth-trend-start").inner_text() == "2026-10-28"
        assert page.locator("#breadth-trend-svg path").count() == 1
        history_text=page.locator("#breadth-history").inner_text()
        assert "已核验2个交易日" in history_text
        assert "tests.fixture.breadth" in history_text
        assert "不能" not in history_text
        linkage_text=page.locator("#breadth-linkage-results").inner_text()
        assert "+3.50 个百分点" in linkage_text
        assert "上涨比例变化 +8.50 个百分点 − 指数收益 +5.00%" in linkage_text
        assert "2026-10-01 → 2026-10-28" in linkage_text
        assert "Pearson r = 0.620" in linkage_text
        assert "tests.fixture.pe · monthly" in linkage_text
        assert "缺月0" in linkage_text

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
