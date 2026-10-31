"""Manual Playwright smoke test for price-only cross-asset view.

Uses simulated API responses only to validate UI behavior. It does not verify
real AKShare connectivity. Run against a local python app.py serve process.
"""
from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL=os.environ.get("DASHBOARD_URL","http://127.0.0.1:8765")
SCREENSHOT_DIR=Path(__file__).resolve().parents[1]/"artifacts"


def fixture(asset):
    meta={
        "000688":{"name":"科创50","unit":"点","type":"equity_index"},
        "399673":{"name":"创业板50","unit":"点","type":"equity_index"},
        "518880":{"name":"华安黄金ETF","unit":"元/份","type":"gold_etf"},
    }[asset]
    end=date(2026,10,30)
    first=date(2023,1,2)
    count=(end-first).days
    rows=[{"date":(first+timedelta(days=i)).isoformat(),"close":100+i*.04}
          for i in range(count+1) if (first+timedelta(days=i)).weekday()<5]
    stats={
        "3m":{"available":True,"start":"2026-07-30","end":"2026-10-30","samples":67,"return_pct":2.5,"max_drawdown_pct":-4.1,"last_close":rows[-1]["close"]},
        "1y":{"available":True,"start":"2025-10-30","end":"2026-10-30","samples":261,"return_pct":8,"max_drawdown_pct":-5,"last_close":rows[-1]["close"]},
        "10y":{"available":False,"reason":"历史记录不足以覆盖10y"},
        "all":{"available":True,"start":rows[0]["date"],"end":rows[-1]["date"],"samples":len(rows),"return_pct":20,"max_drawdown_pct":-8,"last_close":rows[-1]["close"]},
    }
    return {"asset":asset,"meta":meta,"available":True,"rows":rows,"stats":stats,
        "source":{"first_date":rows[0]["date"],"last_date":rows[-1]["date"],
            "source_name":"Playwright fixture 非真实行情","source_interface":"tests.asset_fixture",
            "source_version":"offline","method":"不代表真实交易日",
            "fetched_at":"2026-10-31T15:00:00+00:00"}}


def main():
    errors=[]
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":1440,"height":900})
        page.on("pageerror",lambda e:errors.append(str(e)))
        page.route("**/api/assets?asset=**",lambda route:route.fulfill(
            status=200,content_type="application/json",
            body=json.dumps(fixture(route.request.url.split("asset=")[-1]),ensure_ascii=False)))
        page.goto(BASE_URL+"/assets.html",wait_until="networkidle")
        page.wait_for_function("document.getElementById('asset-chart')._fullLayout?.xaxis")
        assert "科创50" in page.locator("#asset-title").inner_text()
        assert page.locator("#asset-controls").count()==0 or page.locator("#asset-tabs").is_visible()
        assert page.locator("#asset-close").inner_text().endswith("点")
        page.locator("button[data-asset='518880']").click()
        page.wait_for_function("document.getElementById('asset-title').textContent.includes('华安黄金ETF')")
        assert page.locator("#asset-close").inner_text().endswith("元/份")
        page.locator("button[data-window='10y']").click()
        assert page.locator("#asset-empty").is_visible()
        assert "历史" in page.locator("#asset-empty").inner_text()
        page.locator("button[data-window='all']").click()
        page.wait_for_function("!document.getElementById('asset-chart').hidden")
        page.locator("#asset-panel").screenshot(path=SCREENSHOT_DIR/"v09-assets-desktop.png") if page.locator("#asset-panel").count() else page.screenshot(path=SCREENSHOT_DIR/"v09-assets-desktop.png")
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_timeout(250)
        assert page.locator("#asset-tabs").is_visible()
        assert page.locator("#asset-windows").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+4")
        page.screenshot(path=SCREENSHOT_DIR/"v09-assets-mobile.png")
        assert not errors,errors
        browser.close()
    print("v0.9 price-only asset watch UI smoke tests passed (mock data only)")


if __name__=="__main__":
    main()
