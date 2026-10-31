#!/usr/bin/env python3
"""A-share valuation dashboard: dependency-free local server and SQLite store.

Optional: pip install akshare to fetch historical valuation observations.
Run: python app.py (browser: http://127.0.0.1:8765)
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import math
import mimetypes
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

from data_sources import (
    diagnose_index_source,
    diagnose_source,
    get_update_status,
    import_history,
    read_active_history,
    read_index_history,
    update_history,
    update_index_history,
)
from data_sources.storage import connect as storage_connect

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB = ROOT / "valuation.sqlite3"
INDICES = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000016": "上证50",
}
METRICS = {"pe_ttm": "滚动市盈率（TTM）", "pb": "市净率（PB）"}
COMPARISON_INDICES = ("000016", "000300", "000905", "000852")


def connect():
    return storage_connect(DB)


def canonical_date(raw):
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    v = str(raw).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"无法识别日期: {raw}")


def save_rows(index_code, metric, rows, source):
    if index_code not in INDICES or metric not in METRICS:
        raise ValueError("不支持的指数或指标")
    return import_history(index_code, metric, rows, source, db_path=DB)["count"]


def get_rows(index_code, metric):
    rows, _ = read_active_history(DB, index_code, metric)
    return rows


def get_status():
    return get_update_status(db_path=DB)


def demo_rows(index_code, metric):
    """Deterministic artificial data; never persist or label as financial observations."""
    start = date(2018, 1, 5)
    end = date(2025, 12, 26)
    basis = {"000300": 12.6, "000905": 23.0, "000852": 34.5, "000016": 10.3}[index_code]
    scale = 1 if metric == "pe_ttm" else 0.17
    points = []
    d = start
    while d <= end:
        t = (d - start).days / 365.25
        cycle = 1.05 * math.sin(t * 1.35 - 1.0) + .36 * math.sin(t * 7.1)
        peak = 4.15 * math.exp(-((t - 2.6) / .65) ** 2)
        valley = -1.7 * math.exp(-((t - 4.9) / .8) ** 2)
        lift = .85 * max(0, t - 6.4)
        value = (basis + cycle + peak + valley + lift) * scale
        points.append({"date": d.isoformat(), "value": round(value, 4),
                       "source": "仅作界面演示的合成示例", "fetched_at": None})
        d += timedelta(days=7)
    return points


def read_csv_data(text, metric):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None:
        raise ValueError("CSV需要表头")
    normal = {name.strip().lower(): name for name in reader.fieldnames if name}
    date_candidates = ["date", "日期", "trade_date", "交易日期", "时间"]
    val_candidates = (["value", "滚动市盈率", "pe_ttm", "市盈率ttm", "ttm_pe"]
                      if metric == "pe_ttm" else ["value", "市净率", "pb", "pb_mrq"])
    dcol = next((normal.get(c.lower()) for c in date_candidates if c.lower() in normal), None)
    vcol = next((normal.get(c.lower()) for c in val_candidates if c.lower() in normal), None)
    if not dcol or not vcol:
        raise ValueError(f"缺少列：日期(date/日期) 和 {('滚动市盈率' if metric=='pe_ttm' else '市净率')}(value)")
    return [(r.get(dcol), r.get(vcol)) for r in reader]


def update_akshare(index_code, metric):
    if index_code not in INDICES or metric not in METRICS:
        raise ValueError("不支持的指数或指标")
    return update_history(index_code, metric, db_path=DB)


def update_market(index_code, start_date="20160101", end_date=None):
    if index_code not in INDICES:
        raise ValueError("不支持的指数")
    return update_index_history(index_code, db_path=DB, start_date=start_date, end_date=end_date)


def get_comparison_data(metric):
    if metric not in METRICS:
        raise ValueError("不支持的估值指标")
    series = []
    date_sets = []
    for index_code in COMPARISON_INDICES:
        rows, status = read_active_history(DB, index_code, metric)
        if rows:
            date_sets.append({row["date"] for row in rows})
        series.append({
            "index": index_code,
            "name": INDICES[index_code],
            "rows": rows,
            "status": status,
            "available": bool(rows),
        })
    all_available = all(item["available"] for item in series)
    common_dates = set.intersection(*date_sets) if all_available and date_sets else set()
    common_date = max(common_dates) if common_dates else None
    interfaces = {item["status"].get("source_interface") for item in series if item["available"] and item["status"]}
    methodologies = {item["status"].get("methodology") for item in series if item["available"] and item["status"]}
    missing = [item["name"] for item in series if not item["available"]]
    warnings = []
    if missing:
        warnings.append(f"缺少真实估值序列：{'、'.join(missing)}；未使用演示数据参与比较")
    if all_available and not common_date:
        warnings.append("四个指数没有共同估值日期，无法进行严格同日比较")
    if len(interfaces) > 1 or len(methodologies) > 1:
        warnings.append("存在不同数据来源或估值口径，比较结果不可直接排名")
    return {
        "metric": metric,
        "common_date": common_date,
        "strict_comparison": bool(all_available and common_date and len(interfaces) == 1 and len(methodologies) == 1),
        "source_consistent": len(interfaces) <= 1 and len(methodologies) <= 1,
        "warnings": warnings,
        "series": series,
    }


def response(handler, status, data):
    body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path)
        params = parse_qs(p.query)
        if p.path == "/api/status":
            return response(self, 200, {"indices": INDICES, "metrics": METRICS, "history": get_status()})
        if p.path == "/api/comparison":
            metric = params.get("metric", ["pe_ttm"])[0]
            if metric not in METRICS:
                return response(self, 400, {"error": "不支持的估值指标"})
            return response(self, 200, get_comparison_data(metric))
        if p.path == "/api/history":
            index_code = params.get("index", ["000300"])[0]
            metric = params.get("metric", ["pe_ttm"])[0]
            if index_code not in INDICES or metric not in METRICS:
                return response(self, 400, {"error": "不支持的指数或指标"})
            rows, data_status = read_active_history(DB, index_code, metric)
            market_rows, market_status = read_index_history(DB, index_code)
            demo = not rows
            # Empty database shows a DEMO, explicitly labeled as non-real data.
            if demo:
                rows = demo_rows(index_code, metric)
                data_status = {
                    **(data_status or {}),
                    "data_state": "update_failed" if data_status and data_status.get("last_update_status") == "failed" else "demo",
                    "source_name": "仅作界面演示的合成示例",
                    "source_interface": "local.demo_rows",
                    "methodology": "确定性合成曲线，仅用于界面演示，不是市场数据",
                    "frequency": "weekly",
                    "count": len(rows), "first_date": rows[0]["date"], "last_date": rows[-1]["date"],
                    "fetched_at": None, "has_gaps": False, "missing_periods": 0,
                    "window_support": {"3": True, "5": True, "8": False, "10": False},
                }
            return response(self, 200, {"index": index_code, "name": INDICES[index_code],
                                         "metric": metric, "is_demo": demo, "status": data_status, "rows": rows,
                                         "market_rows": market_rows, "market_status": market_status})
        requested = "index.html" if p.path == "/" else p.path.lstrip("/")
        candidate = (STATIC / requested).resolve()
        if not candidate.is_relative_to(STATIC.resolve()) or not candidate.is_file():
            self.send_error(404, "Not found")
            return
        data = candidate.read_bytes()
        kind = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        p = urlparse(self.path)
        params = parse_qs(p.query)
        index_code = params.get("index", ["000300"])[0]
        metric = params.get("metric", ["pe_ttm"])[0]
        if index_code not in INDICES or metric not in METRICS:
            return response(self, 400, {"error": "不支持的指数或指标"})
        size = int(self.headers.get("Content-Length", "0"))
        if size > 3_000_000:
            return response(self, 413, {"error": "CSV文件不能超过3MB"})
        try:
            if p.path == "/api/import":
                content = self.rfile.read(size).decode("utf-8-sig")
                rows = read_csv_data(content, metric)
                result = import_history(index_code, metric, rows, "用户CSV导入（需自行核对口径）", db_path=DB)
                return response(self, 200, {**result, "message": f"成功写入 {result['count']} 条有效记录"})
            if p.path == "/api/update":
                result = update_akshare(index_code, metric)
                return response(self, 200, result)
            if p.path == "/api/update-market":
                result = update_market(index_code)
                return response(self, 200, result)
            return response(self, 404, {"error": "未知接口"})
        except Exception as e:
            return response(self, 422, {"error": str(e)})


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(ROOT / "data_updates.log", encoding="utf-8"), logging.StreamHandler()],
    )
    parser = argparse.ArgumentParser(description="本地 A 股估值仪表盘")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="启动本地页面（默认）")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", default="127.0.0.1")
    update = sub.add_parser("update", help="拉取真实历史估值（需akshare）")
    update.add_argument("--index", choices=list(INDICES) + ["all"], default="000300")
    update.add_argument("--metric", choices=list(METRICS) + ["all"], default="pe_ttm")
    diagnose = sub.add_parser("diagnose", help="联网诊断 AKShare 返回结构，不写入数据库")
    diagnose.add_argument("--index", choices=["000300"], default="000300")
    diagnose.add_argument("--metric", choices=list(METRICS) + ["all"], default="all")
    update_market_parser = sub.add_parser("update-market", help="拉取指数自身的日线收盘点位（需akshare）")
    update_market_parser.add_argument("--index", choices=list(INDICES) + ["all"], default="000300")
    update_market_parser.add_argument("--start", default="20160101")
    update_market_parser.add_argument("--end", default=None)
    diagnose_market = sub.add_parser("diagnose-market", help="联网诊断指数行情接口，不写入数据库")
    diagnose_market.add_argument("--index", choices=INDICES, default="000300")
    diagnose_market.add_argument("--start", default="20160101")
    diagnose_market.add_argument("--end", default=None)
    imp = sub.add_parser("import", help="从CSV导入历史数据")
    imp.add_argument("--file", required=True)
    imp.add_argument("--index", choices=INDICES, default="000300")
    imp.add_argument("--metric", choices=METRICS, default="pe_ttm")
    args = parser.parse_args()
    if args.command == "update":
        codes = INDICES if args.index == "all" else [args.index]
        metrics = METRICS if args.metric == "all" else [args.metric]
        failed = 0
        for code in codes:
            for metric in metrics:
                try:
                    print(INDICES[code], metric, json.dumps(update_akshare(code, metric), ensure_ascii=False))
                except Exception as e:
                    failed += 1
                    print("更新失败：", INDICES[code], metric, e, file=sys.stderr)
        return 1 if failed else 0
    elif args.command == "update-market":
        failed = 0
        codes = INDICES if args.index == "all" else [args.index]
        for code in codes:
            try:
                print(INDICES[code], json.dumps(update_market(code, args.start, args.end), ensure_ascii=False))
            except Exception as exc:
                failed += 1
                print("指数行情更新失败：", INDICES[code], exc, file=sys.stderr)
        return 1 if failed else 0
    elif args.command == "diagnose-market":
        try:
            result = diagnose_index_source(args.index, start_date=args.start, end_date=args.end)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"指数行情诊断失败：{INDICES[args.index]}: {exc}", file=sys.stderr)
            return 1
    elif args.command == "diagnose":
        failed = 0
        metrics = METRICS if args.metric == "all" else [args.metric]
        for metric in metrics:
            try:
                result = diagnose_source(args.index, metric)
                print(f"\n[{INDICES[args.index]} {metric}]")
                print(json.dumps(result, ensure_ascii=False, indent=2))
            except Exception as e:
                failed += 1
                print(f"诊断失败：{INDICES[args.index]} {metric}: {e}", file=sys.stderr)
        return 1 if failed else 0
    elif args.command == "import":
        text = Path(args.file).read_text(encoding="utf-8-sig")
        try:
            result = import_history(args.index, args.metric, read_csv_data(text, args.metric),
                                    "用户CSV导入（需自行核对口径）", db_path=DB)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"导入失败：{exc}", file=sys.stderr)
            return 1
    else:
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8765)
        connect().close()
        print(f"打开浏览器：http://{host}:{port}   （Ctrl+C 结束）")
        try:
            ThreadingHTTPServer((host, port), Handler).serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
