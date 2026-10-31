"""Playwright acceptance test for v0.5 controls, comparison and shared axes."""

from __future__ import annotations

from datetime import date, timedelta
import json
import math
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8765")
ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"
INDEX_NAMES = {"000016": "上证50", "000300": "沪深300", "000905": "中证500", "000852": "中证1000"}


def market_fixture():
    rows, current, sequence = [], date(2016, 1, 4), 0
    while current <= date(2026, 10, 30):
        if current.weekday() < 5:
            value = 3400 + sequence * .38 + 380 * math.sin(sequence / 160) + 90 * math.sin(sequence / 31)
            rows.append({"date": current.isoformat(), "value": round(value, 2), "source": "浏览器测试模拟响应", "fetched_at": "2026-10-31T08:00:00+00:00"})
            sequence += 1
        current += timedelta(days=1)
    return rows


def valuation_fixture(index_code, metric):
    bases = {"000016": 10.5, "000300": 12.5, "000905": 23.0, "000852": 34.0}
    scale = 1 if metric == "pe_ttm" else .11
    rows = []
    for offset in range(154):
        month_index = 2014 * 12 + offset
        year, month_zero = divmod(month_index, 12)
        value = (bases[index_code] + 1.4 * math.sin(offset / 9) + .5 * math.sin(offset / 2.7)) * scale
        rows.append({"date": date(year, month_zero + 1, 28).isoformat(), "value": round(value, 4), "source": "浏览器测试模拟响应", "fetched_at": "2026-10-31T08:00:00+00:00"})
    return rows


def status_fixture(index_code, metric, rows):
    column = "滚动市盈率" if metric == "pe_ttm" else "市净率"
    return {
        "data_state": "ready", "source_name": "浏览器测试模拟响应（非市场数据）",
        "source_interface": f"tests.fixture.{metric}", "methodology": f"自动化测试统一{column}口径；不写入 SQLite",
        "frequency": "monthly", "count": len(rows), "first_date": rows[0]["date"], "last_date": rows[-1]["date"],
        "fetched_at": "2026-10-31T08:00:00+00:00", "has_gaps": False, "missing_periods": 0,
        "window_support": {"3": True, "5": True, "8": True, "10": True},
    }


def decomposition_fixture(index_code, metric, horizon):
    label = "隐含盈利代理" if metric == "pe_ttm" else "隐含净资产代理"
    scale = 1 if metric == "pe_ttm" else .15
    series = []
    multiple_change = .35 * scale
    for offset in range(37):
        year, month_zero = divmod(2023 * 12 + 9 + offset, 12)
        progress = offset / 36
        normalized_index = 100 * 1.2 ** progress
        normalized_multiple = 100 * (1 + multiple_change) ** progress
        series.append({
            "date": date(year, month_zero + 1, 28).isoformat(),
            "index": round(normalized_index, 6),
            "multiple": round(normalized_multiple, 6),
            "implied": round(100 * normalized_index / normalized_multiple, 6),
        })
    implied_change = 1.2 / (1 + multiple_change) - 1
    close_log = 100 * math.log(1.2)
    multiple_log = 100 * math.log(1 + multiple_change)
    return {
        "index": index_code, "name": INDEX_NAMES[index_code], "metric": metric, "horizon": horizon,
        "label": label, "start": series[0]["date"], "end": series[-1]["date"], "matched_count": 760,
        "lag_days": 3, "price_source": "浏览器测试模拟指数响应", "valuation_source": "浏览器测试模拟估值响应",
        "method": "log-return, exact: test fixture", "warnings": ["自动化测试：隐含代理不是实际利润或净资产"],
        "changes_pct": {"close": 20, "multiple": 100 * multiple_change, "implied": 100 * implied_change},
        "log_contributions_pp": {"close": close_log, "multiple": multiple_log, "implied": close_log - multiple_log},
        "series": series,
    }


def coverage_fixture():
    indices = []
    for code in ("000016", "000300", "000905", "000852"):
        metrics = {}
        for metric in ("pe_ttm", "pb", "index_close"):
            ready = code == "000300" and metric in ("pe_ttm", "pb", "index_close")
            metrics[metric] = {
                "count": 259 if ready and metric != "index_close" else 2628 if ready else 0,
                "frequency": "daily" if ready and metric == "index_close" else "monthly" if ready else "unknown",
                "first_date": "2016-01-04" if ready else None,
                "last_date": "2026-10-30" if ready else None,
                "source_name": "浏览器测试模拟响应（非市场数据）" if ready else None,
                "source_interface": f"tests.fixture.{metric}" if ready else None,
                "data_state": "ready" if ready else "no_data",
                "missing_reason": None if ready else "尚未采集真实数据",
            }
        indices.append({"index": code, "name": INDEX_NAMES[code], "metrics": metrics})
    return {"indices": indices, "summary": {"available": 3, "total": 12}}


def single_state(page):
    return page.evaluate("""() => {
        const chart = document.getElementById('chart');
        return {
            dtick: chart._fullLayout.xaxis2.dtick,
            minor: chart._fullLayout.xaxis2.minor && chart._fullLayout.xaxis2.minor.dtick,
            range: chart._fullLayout.xaxis2.range.map(value => String(value).slice(0, 10)),
            topRange: chart._fullLayout.xaxis.range.map(value => String(value).slice(0, 10)),
            stats: ['current','pct','z','mean','stats-range','stats-count'].map(id => document.getElementById(id).textContent),
            title: document.getElementById('chart-title').textContent,
            index: document.getElementById('idx').value,
            maximized: document.getElementById('analysis-card').classList.contains('analysis-maximized'),
        };
    }""")


def tick_overlaps(page):
    return page.evaluate("""() => {
        const boxes = [...document.querySelectorAll('#chart .xaxislayer-above .x2tick text')]
            .map(node => node.getBoundingClientRect()).filter(box => box.width > 0)
            .sort((a,b) => a.left-b.left);
        return boxes.slice(1).filter((box,index) => box.left < boxes[index].right-1).length;
    }""")


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    market_rows = market_fixture()
    fixture_mode = {"missing": False}
    page_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1350}, device_scale_factor=1)
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def route_history(route):
            parsed = urlparse(route.request.url)
            params = parse_qs(parsed.query)
            index_code = params.get("index", ["000300"])[0]
            metric = params.get("metric", ["pe_ttm"])[0]
            rows = valuation_fixture(index_code, metric)
            payload = {
                "index": index_code, "name": INDEX_NAMES[index_code], "metric": metric, "is_demo": False,
                "status": status_fixture(index_code, metric, rows), "rows": rows,
                "market_rows": market_rows,
                "market_status": {
                    "data_state": "ready", "source_name": "浏览器测试模拟响应（非市场数据）",
                    "source_interface": "tests.fixture.index_close", "methodology": "自动化测试指数收盘；不写入 SQLite",
                    "frequency": "daily", "count": len(market_rows), "first_date": market_rows[0]["date"], "last_date": market_rows[-1]["date"],
                    "fetched_at": "2026-10-31T08:00:00+00:00", "has_gaps": False, "missing_periods": 0,
                },
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

        def route_comparison(route):
            metric = parse_qs(urlparse(route.request.url).query).get("metric", ["pe_ttm"])[0]
            series = []
            for code in ("000016", "000300", "000905", "000852"):
                rows = valuation_fixture(code, metric)
                available = not (fixture_mode["missing"] and code == "000852")
                series.append({"index": code, "name": INDEX_NAMES[code], "available": available,
                               "rows": rows if available else [], "status": status_fixture(code, metric, rows) if available else None})
            strict = not fixture_mode["missing"]
            payload = {"metric": metric, "common_date": "2026-10-28" if strict else None,
                       "strict_comparison": strict, "source_consistent": True,
                       "warnings": [] if strict else ["缺少真实估值序列：中证1000；未使用演示数据参与比较"], "series": series}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

        def route_decomposition(route):
            params = parse_qs(urlparse(route.request.url).query)
            index_code = params.get("index", ["000300"])[0]
            metric = params.get("metric", ["pe_ttm"])[0]
            horizon = params.get("horizon", ["3y"])[0]
            if horizon == "10y":
                payload = {"error": "10y 历史覆盖不足或起点附近没有同日观测", "index": index_code, "metric": metric, "horizon": horizon}
                route.fulfill(status=422, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(decomposition_fixture(index_code, metric, horizon), ensure_ascii=False))

        page.route("**/api/history?**", route_history)
        page.route("**/api/comparison?**", route_comparison)
        page.route("**/api/decomposition?**", route_decomposition)
        page.route("**/api/coverage", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(coverage_fixture(), ensure_ascii=False)
        ))
        response = page.goto(BASE_URL, wait_until="networkidle")
        assert response and "no-cache" in response.headers.get("cache-control", "")
        page.wait_for_function("document.getElementById('chart')._fullLayout?.xaxis2")

        controls_box = page.locator("#chart-controls").bounding_box()
        coverage_box = page.locator("#data-coverage-panel").bounding_box()
        chart_box = page.locator("#chart").bounding_box()
        assert controls_box and coverage_box and chart_box
        assert controls_box["y"] < coverage_box["y"] < chart_box["y"]
        assert chart_box["y"] - (coverage_box["y"] + coverage_box["height"]) < 30
        assert page.locator("#coverage-body tr").count() == 4
        assert page.locator("#coverage-summary").text_content() == "3 / 12 个真实序列"
        assert "尚未采集真实数据" in page.locator("#coverage-body").text_content()
        page.locator("#data-coverage-panel").screenshot(path=ARTIFACTS / "v0.7-data-coverage.png")
        initial = single_state(page)
        assert initial["dtick"] == "M6", initial
        assert tick_overlaps(page) == 0
        page.locator("#analysis-card").screenshot(path=ARTIFACTS / "v0.5-single-controls.png")

        page.locator("button[data-window='10y']").click()
        page.wait_for_function("document.getElementById('chart')._fullLayout.xaxis2.dtick === 'M6'")
        ten_year_stats = single_state(page)["stats"]
        page.evaluate("Plotly.relayout('chart', {'xaxis2.range':['2026-07-30','2026-10-30']})")
        page.wait_for_function("document.getElementById('chart')._fullLayout.xaxis2.dtick === 604800000")
        zoomed = single_state(page)
        assert zoomed["minor"] == 86400000, zoomed
        assert zoomed["range"] == zoomed["topRange"]
        assert zoomed["stats"] == ten_year_stats
        page.locator("#reset-zoom").click()
        page.wait_for_function("document.getElementById('chart')._fullLayout.xaxis2.dtick === 'M6'")
        reset_state = single_state(page)
        assert reset_state["stats"] == ten_year_stats

        page.locator("button[data-mode='drivers']").click()
        page.wait_for_function("document.getElementById('drivers-series-chart').data?.length === 3")
        assert "760 个同日样本" in page.locator("#drivers-date").text_content()
        assert page.locator("#drivers-warning").is_visible()
        assert page.locator(".cards").is_hidden()
        page.locator("button[data-metric='pb']").click()
        page.wait_for_function("document.getElementById('drivers-implied-label').textContent.includes('净资产')")
        page.locator("button[data-horizon='10y']").click()
        page.wait_for_function("document.getElementById('drivers-warning').textContent.includes('历史覆盖不足')")
        assert page.evaluate("document.getElementById('drivers-series-chart').data.length") == 0
        assert "未使用演示数据" in page.locator("#data-source").text_content()
        page.locator("button[data-horizon='3y']").click()
        page.wait_for_function("document.getElementById('drivers-series-chart').data?.length === 3")
        page.select_option("#idx", "000905")
        page.wait_for_function("document.getElementById('drivers-title').textContent.includes('中证500')")
        page.locator("#maximize-chart").click()
        page.wait_for_function("document.getElementById('analysis-card').classList.contains('analysis-maximized')")
        assert page.locator("#drivers-series-chart").is_visible() and page.locator("#drivers-bars-chart").is_visible()
        page.locator("#analysis-card").screenshot(path=ARTIFACTS / "v0.6-decomposition.png")
        page.locator("#maximize-chart").click()
        page.wait_for_function("!document.getElementById('analysis-card').classList.contains('analysis-maximized')")

        page.locator("button[data-mode='compare']").click()
        page.wait_for_function("document.getElementById('compare-chart').data?.[0]?.x?.length === 4")
        assert page.locator("#comparison-table-body tr[data-index]").count() == 4
        assert page.evaluate("document.getElementById('compare-chart')._fullLayout.xaxis.range") == [0, 100]
        assert page.locator("#comparison-warning").is_hidden()
        page.locator("#analysis-card").screenshot(path=ARTIFACTS / "v0.5-comparison.png")

        page.locator("button[data-metric='pb']").click()
        page.wait_for_function("document.getElementById('chart-title').textContent.includes('PB')")
        page.locator("button[data-stats-window='5y']").click()
        page.wait_for_function("document.getElementById('stats-count').textContent.includes('5年')")
        assert page.locator("#comparison-table-body tr[data-index]").count() == 4

        fixture_mode["missing"] = True
        page.locator("button[data-metric='pe_ttm']").click()
        page.wait_for_function("document.getElementById('comparison-warning').textContent.includes('中证1000')")
        assert page.locator("#comparison-warning").is_visible()
        assert "不参与比较" in page.locator("#comparison-table-body").text_content()
        assert page.evaluate("document.getElementById('compare-chart').data.length") == 0
        assert "比较条形图已排除" in page.locator("#comparison-warning").text_content()
        fixture_mode["missing"] = False
        page.locator("button[data-metric='pb']").click()
        page.wait_for_function("document.getElementById('compare-chart').data?.[0]?.x?.length === 4")

        page.locator("#comparison-table-body tr[data-index='000905']").click()
        page.wait_for_function("document.getElementById('idx').value === '000905' && document.getElementById('chart')._fullLayout?.xaxis2")
        drilled = single_state(page)
        assert drilled["index"] == "000905" and "市净率 PB" in drilled["title"]
        assert "5年" in drilled["stats"][-1]

        before_max = drilled["range"]
        page.locator("#maximize-chart").click()
        page.wait_for_function("document.getElementById('analysis-card').classList.contains('analysis-maximized')")
        maximized = single_state(page)
        assert maximized["range"] == before_max
        assert page.locator("#chart-controls").is_visible() and page.locator("#stats-control").is_visible()
        page.locator("#analysis-card").screenshot(path=ARTIFACTS / "v0.5-maximized.png")
        page.locator("#maximize-chart").click()
        page.wait_for_function("!document.getElementById('analysis-card').classList.contains('analysis-maximized')")
        assert single_state(page)["range"] == before_max

        page.set_viewport_size({"width": 390, "height": 1100})
        page.wait_for_timeout(350)
        assert page.locator("#chart-controls").bounding_box()["width"] <= 370
        assert page.evaluate("document.getElementById('window').scrollWidth >= document.getElementById('window').clientWidth")
        assert tick_overlaps(page) == 0
        page.locator("#analysis-card").screenshot(path=ARTIFACTS / "v0.5-mobile-controls.png")
        page.locator("button[data-mode='drivers']").click()
        page.wait_for_function("document.getElementById('drivers-series-chart').data?.length === 3")
        assert page.locator("#drivers-series-chart").bounding_box()["width"] <= 370
        page.locator("#analysis-card").screenshot(path=ARTIFACTS / "v0.6-decomposition-mobile.png")
        assert page_errors == [], page_errors
        browser.close()

    print(json.dumps({"initial": initial, "zoomed_three_months": zoomed, "reset": reset_state, "drilled": drilled,
                      "comparison_rows": 4, "fixture_market_count": len(market_rows), "page_errors": page_errors},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
