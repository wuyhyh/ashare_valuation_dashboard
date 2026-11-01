"""Manual Playwright acceptance against locally collected real v0.9 SQLite data.

This script intentionally does not mock API responses and is not run in CI,
because the private SQLite files are excluded from Git. Run it only after
successful ``breadth-update`` and ``assets-update`` commands.
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8765")
ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


def main() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))

        page.goto(BASE_URL + "/breadth.html", wait_until="networkidle")
        page.locator("#breadth-report").wait_for(state="visible")
        breadth = page.evaluate("fetch('/api/breadth').then(response => response.json())")
        assert breadth["available"]
        assert breadth["capture"]["source_interface"] in {
            "ak.stock_zh_a_spot_em", "ak.stock_zh_a_spot_tx", "ak.stock_zh_a_spot",
        }
        pool = breadth["capture"]["quality"]["official_pool"]
        assert breadth["capture"]["coverage_scope"].startswith("sh_sz_all_a")
        assert all(pool["boards"][board]["coverage_ratio"] >= .95
                   for board in ("SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"))
        sh_sz_official = sum(pool["boards"][board]["official_count"]
                             for board in ("SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"))
        assert breadth["groups"]["SH_SZ_ALL_all"]["identified_count"] == sh_sz_official
        assert breadth["groups"]["MAIN_ALL_all"]["identified_count"] < sh_sz_official
        assert breadth["groups"]["CHINEXT_all"]["identified_count"] > 1000
        assert breadth["groups"]["STAR_all"]["identified_count"] > 500
        assert pool["special_security_count"] >= 1
        if "BSE" in breadth["available_scopes"]:
            assert pool["boards"]["BSE"]["coverage_ratio"] >= .95
            page.locator("#breadth-exchange").select_option("BSE")
            assert page.locator("#b-coverage").inner_text() == "100.0%"
        page.locator("#breadth-exchange").select_option("SH_SZ_ALL")
        assert "未验证" in page.locator("#breadth-date-trust").inner_text()
        linkage = page.evaluate(
            "fetch('/api/breadth-linkage?scope=SH_SZ_ALL&exclude_st=0').then(response => response.json())"
        )
        assert linkage["available"] is False and linkage["breadth_points"] == 0
        assert "不足" in page.locator("#breadth-linkage-status").inner_text()
        assert not page.locator("#breadth-linkage-results").is_visible()
        page.screenshot(path=str(ARTIFACTS / "v010-real-breadth-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 4")
        page.screenshot(path=str(ARTIFACTS / "v010-real-breadth-mobile.png"), full_page=True)

        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(BASE_URL + "/assets.html", wait_until="networkidle")
        page.wait_for_function("document.getElementById('asset-chart')._fullLayout?.xaxis")
        for asset_id, expected_unit in (("000688", "点"), ("399673", "点"), ("518880", "元/份")):
            page.locator(f"button[data-asset='{asset_id}']").click()
            page.wait_for_function(
                "asset => document.getElementById('asset-title').textContent.includes(asset)",
                arg={"000688": "科创50", "399673": "创业板50", "518880": "华安黄金ETF"}[asset_id],
            )
            payload = page.evaluate(
                "asset => fetch('/api/assets?asset=' + asset).then(response => response.json())",
                asset_id,
            )
            assert payload["available"] and payload["rows"]
            assert payload["source"]["verification"]["status"] == "verified"
            assert page.locator("#asset-close").inner_text().endswith(expected_unit)
            assert "交叉核验通过" in page.locator("#asset-verification").inner_text()

        page.locator("button[data-asset='399673']").click()
        page.locator("button[data-window='10y']").click()
        page.wait_for_function("!document.getElementById('asset-chart').hidden")
        page.evaluate(
            "Plotly.relayout(document.getElementById('asset-chart'), "
            "{'xaxis.range[0]':'2026-07-30','xaxis.range[1]':'2026-10-30'})"
        )
        page.wait_for_function("document.getElementById('asset-chart').layout.xaxis.dtick === 604800000")
        assert page.evaluate("document.getElementById('asset-chart').layout.xaxis.dtick") == 604800000
        assert page.evaluate("document.getElementById('asset-chart').layout.xaxis.tickformat") == "%m-%d"
        labels = page.locator("#asset-chart .xaxislayer-above .xtick text").evaluate_all(
            "elements => elements.map(element => element.textContent)"
        )
        assert len(set(labels)) == len(labels), labels
        page.screenshot(path=str(ARTIFACTS / "v09-real-assets-desktop.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("document.getElementById('asset-chart').layout.xaxis.dtick === 1209600000")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 4")
        assert page.locator("#asset-tabs").is_visible()
        assert page.locator("#asset-windows").is_visible()
        page.screenshot(path=str(ARTIFACTS / "v09-real-assets-mobile.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("v0.10 real SQLite Playwright acceptance passed")


if __name__ == "__main__":
    main()
