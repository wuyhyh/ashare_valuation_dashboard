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
import os
import sqlite3
import subprocess
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlparse

from data_sources import (
    diagnose_index_source,
    diagnose_source,
    get_update_status,
    import_history,
    import_index_history,
    PROVIDER_CHOICES,
    PROVIDER_ORDER,
    read_active_history,
    read_index_history,
    update_history,
    update_index_history,
)
from data_sources.storage import connect as storage_connect
from decomposition import analyze

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


def get_coverage_data():
    metrics = ("pe_ttm", "pb", "index_close")
    indices = []
    available = 0
    for index_code in COMPARISON_INDICES:
        metric_status = {}
        for metric in metrics:
            rows, status = read_active_history(DB, index_code, metric)
            status = status or {}
            if rows:
                available += 1
                if status.get("last_update_status") == "failed":
                    missing_reason = status.get("last_error") or "最近一次更新失败，继续使用原有历史"
                elif status.get("data_state") == "stale":
                    missing_reason = "数据已过期，请检查上游更新"
                elif status.get("data_state") == "insufficient":
                    missing_reason = "历史覆盖不足部分长期统计窗口"
                else:
                    missing_reason = None
            else:
                missing_reason = status.get("last_error") or "尚未采集真实数据"
            metric_status[metric] = {
                "count": len(rows),
                "frequency": status.get("frequency") or "unknown",
                "first_date": status.get("first_date"),
                "last_date": status.get("last_date"),
                "source_name": status.get("source_name"),
                "source_interface": status.get("source_interface"),
                "source_identity": status.get("source_identity"),
                "data_state": status.get("data_state") or "no_data",
                "missing_reason": missing_reason,
            }
        indices.append({"index": index_code, "name": INDICES[index_code], "metrics": metric_status})
    return {
        "indices": indices,
        "summary": {"available": available, "total": len(COMPARISON_INDICES) * len(metrics)},
    }


def _git_value(*args):
    try:
        process = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True,
            timeout=2, check=False,
        )
        return process.stdout.strip() if process.returncode == 0 else None
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def get_runtime_info():
    """Report the source tree and local SQLite actually served by THIS process."""
    branch = _git_value("branch", "--show-current")
    sha = _git_value("rev-parse", "--short=12", "HEAD")
    db_exists = DB.is_file()
    coverage = get_coverage_data() if db_exists else None
    summary = dict(coverage["summary"]) if coverage else {
        "available": 0, "total": len(COMPARISON_INDICES) * 3
    }
    if coverage:
        metrics = [status for index in coverage["indices"] for status in index["metrics"].values()]
        cutoffs = [item["last_date"] for item in metrics if item.get("last_date")]
        summary["oldest_cutoff"] = min(cutoffs) if cutoffs else None
        summary["newest_cutoff"] = max(cutoffs) if cutoffs else None
        summary["stale_or_failed"] = sum(
            item.get("data_state") in ("stale", "update_failed") for item in metrics
        )
    return {
        "version": "v0.8-dev",
        "branch": branch or "unknown",
        "commit": sha or "unknown",
        "project_root": str(ROOT.resolve()),
        "database_path": str(DB.resolve()),
        "database_exists": db_exists,
        "process_id": os.getpid(),
        "coverage": summary,
        "note": "启动应用不会联网更新。新克隆仓库不包含 valuation.sqlite3；运行 python app.py sync 可显式采集。",
    }


def _sqlite_backup_for_sync():
    """Use SQLite's online backup, not a raw copy of a possibly active WAL file."""
    if not DB.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = DB.with_name(f"{DB.name}.backup-before-sync-{stamp}")
    with sqlite3.connect(DB) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    return backup


MARKET_SYNC_START = {
    "000016": "20050101",
    "000300": "20160101",
    "000905": "20070101",
    "000852": "20140101",
}


def sync_all_real_data(*, provider="csindex_official", only_missing=False, pause_seconds=2.0):
    """Explicit, foreground 12-series update; no silent demo and no auto-start fetch."""
    if provider not in ("auto", *PROVIDER_CHOICES):
        raise ValueError(f"不支持的数据源: {provider}")
    if pause_seconds < 0:
        raise ValueError("请求间隔不能小于0")
    backup = _sqlite_backup_for_sync()
    if backup:
        print(f"本地 SQLite 已备份：{backup}", flush=True)
    results = []
    for index_code in COMPARISON_INDICES:
        rows, _ = read_index_history(DB, index_code)
        if only_missing and rows:
            results.append({"index": index_code, "metric": "index_close", "status": "skipped"})
            continue
        print(f"更新 {INDICES[index_code]} 指数点位（{provider}）…", flush=True)
        try:
            result = update_market(index_code, start_date=MARKET_SYNC_START[index_code], provider_name=provider)
            results.append({"index": index_code, "metric": "index_close", "status": "success", "last_date": result.get("last_date"), "count": result.get("count")})
        except Exception as exc:
            print(f"指数点位更新失败：{INDICES[index_code]} {exc}", file=sys.stderr, flush=True)
            results.append({"index": index_code, "metric": "index_close", "status": "failed", "error": str(exc)})
    for i, (index_code, metric) in enumerate(
        (code, metric) for code in COMPARISON_INDICES for metric in METRICS
    ):
        rows, _ = read_active_history(DB, index_code, metric)
        if only_missing and rows:
            results.append({"index": index_code, "metric": metric, "status": "skipped"})
            continue
        if i or results:
            time.sleep(pause_seconds)
        print(f"更新 {INDICES[index_code]} {metric}…", flush=True)
        try:
            result = update_akshare(index_code, metric)
            results.append({"index": index_code, "metric": metric, "status": "success", "last_date": result.get("last_date"), "count": result.get("count")})
        except Exception as exc:
            print(f"估值更新失败：{INDICES[index_code]} {metric} {exc}", file=sys.stderr, flush=True)
            results.append({"index": index_code, "metric": metric, "status": "failed", "error": str(exc)})
    return {
        "backup": str(backup) if backup else None,
        "results": results,
        "success": sum(item["status"] == "success" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "coverage": get_coverage_data()["summary"],
    }


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


def update_market(index_code, start_date="20160101", end_date=None, provider_name="auto"):
    if index_code not in INDICES:
        raise ValueError("不支持的指数")
    return update_index_history(index_code, db_path=DB, start_date=start_date, end_date=end_date, provider_name=provider_name)


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
    frequencies = {item["status"].get("frequency") for item in series if item["available"] and item["status"]}
    frequency_consistent = len(frequencies) == 1 and next(iter(frequencies), None) not in (None, "unknown", "irregular")
    identity_results = [
        item["status"].get("source_identity") for item in series
        if item["available"] and item["status"] and item["status"].get("source_identity") is not None
    ]
    identity_consistent = not identity_results or (
        len(identity_results) == len(series)
        and all(result.get("status") == "passed" for result in identity_results)
    )
    missing = [item["name"] for item in series if not item["available"]]
    warnings = []
    if missing:
        warnings.append(f"缺少真实估值序列：{'、'.join(missing)}；未使用演示数据参与比较")
    if all_available and not common_date:
        warnings.append("四个指数没有共同估值日期，无法进行严格同日比较")
    if len(interfaces) > 1 or len(methodologies) > 1:
        warnings.append("存在不同数据来源或估值口径，比较结果不可直接排名")
    if all_available and not frequency_consistent:
        warnings.append("指数估值采样频率不一致或尚未核验，历史分位数不可作为严格同频率比较")
    if all_available and not identity_consistent:
        warnings.append("至少一个估值序列尚未通过其数值指数点位与独立收盘序列的身份交叉核验")
    return {
        "metric": metric,
        "common_date": common_date,
        "strict_comparison": bool(all_available and common_date and len(interfaces) == 1 and len(methodologies) == 1 and frequency_consistent and identity_consistent),
        "source_consistent": len(interfaces) <= 1 and len(methodologies) <= 1,
        "frequency_consistent": frequency_consistent,
        "identity_consistent": identity_consistent,
        "warnings": warnings,
        "series": series,
    }


def _decomposition_context(index_code, metric, asof=None):
    valuation_rows, valuation_status = read_active_history(DB, index_code, metric)
    market_rows, market_status = read_index_history(DB, index_code)
    market_dates = {row["date"] for row in market_rows}
    valuation_dates = {row["date"] for row in valuation_rows}
    frequency = (valuation_status or {}).get("frequency", "unknown")
    readiness = []
    for candidate in ("1y", "3y", "5y", "10y"):
        try:
            result = analyze(
                market_rows, valuation_rows, metric=metric, horizon=candidate,
                index_code=index_code, asof=asof, frequency=frequency,
            )
            readiness.append({
                "horizon": candidate, "status": "computable", "message": "可计算",
                "matched_count": result["matched_count"], "coverage_ratio": result["coverage_ratio"],
            })
        except ValueError as exc:
            message = str(exc)
            if (market_status or {}).get("last_update_status") == "failed" and not market_rows:
                status = "collection_failed"
            elif "历史覆盖不足" in message or "起点" in message:
                status = "history_insufficient"
            else:
                status = "same_date_insufficient"
            readiness.append({"horizon": candidate, "status": status, "message": message})
    return {
        "valuation_rows": valuation_rows, "market_rows": market_rows,
        "frequency": frequency, "valuation_status": valuation_status or {}, "market_status": market_status or {},
        "common_date_count": len(market_dates.intersection(valuation_dates)), "readiness": readiness,
    }


def get_decomposition_data(index_code, metric, horizon, asof=None):
    if index_code not in INDICES or metric not in METRICS:
        raise ValueError("不支持的指数或指标")
    context = _decomposition_context(index_code, metric, asof)
    result = analyze(
        context["market_rows"],
        context["valuation_rows"],
        metric=metric,
        horizon=horizon,
        index_code=index_code,
        asof=asof,
        frequency=context["frequency"],
    )
    result["name"] = INDICES[index_code]
    result["context"] = {key: value for key, value in context.items() if not key.endswith("_rows")}
    return result


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
        if p.path == "/api/runtime":
            return response(self, 200, get_runtime_info())
        if p.path == "/api/status":
            return response(self, 200, {"indices": INDICES, "metrics": METRICS, "history": get_status()})
        if p.path == "/api/coverage":
            return response(self, 200, get_coverage_data())
        if p.path == "/api/comparison":
            metric = params.get("metric", ["pe_ttm"])[0]
            if metric not in METRICS:
                return response(self, 400, {"error": "不支持的估值指标"})
            return response(self, 200, get_comparison_data(metric))
        if p.path == "/api/decomposition":
            index_code = params.get("index", ["000300"])[0]
            metric = params.get("metric", ["pe_ttm"])[0]
            horizon = params.get("horizon", ["3y"])[0]
            asof = params.get("asof", [None])[0]
            if index_code not in INDICES or metric not in METRICS:
                return response(self, 400, {"error": "不支持的指数或指标"})
            try:
                return response(self, 200, get_decomposition_data(index_code, metric, horizon, asof))
            except ValueError as exc:
                context = _decomposition_context(index_code, metric, asof)
                return response(self, 422, {
                    "error": str(exc), "index": index_code, "metric": metric, "horizon": horizon,
                    "context": {key: value for key, value in context.items() if not key.endswith("_rows")},
                })
        if p.path == "/api/history":
            index_code = params.get("index", ["000300"])[0]
            metric = params.get("metric", ["pe_ttm"])[0]
            if index_code not in INDICES or metric not in METRICS:
                return response(self, 400, {"error": "不支持的指数或指标"})
            rows, data_status = read_active_history(DB, index_code, metric)
            market_rows, market_status = read_index_history(DB, index_code)
            # No silent synthetic fallback: only explicit demo=1 may show a fake curve.
            demo = not rows and params.get("demo", ["0"])[0] == "1"
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
                provider_name = params.get("provider", ["auto"])[0]
                result = update_market(index_code, provider_name=provider_name)
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
    doctor = sub.add_parser("doctor", help="无需联网：查看当前源码、SQLite 路径与12条真实数据的覆盖情况")
    sync_parser = sub.add_parser("sync", help="显式联网更新四指数点位及PE/PB；启动服务本身不会更新")
    sync_parser.add_argument("--provider", choices=["auto", *PROVIDER_CHOICES], default="csindex_official")
    sync_parser.add_argument("--only-missing", action="store_true", help="仅采集本地不存在的序列")
    sync_parser.add_argument("--pause", type=float, default=2.0, help="每次估值请求间隔秒数（默认2秒）")
    update = sub.add_parser("update", help="拉取真实历史估值（需akshare）")
    update.add_argument("--index", choices=list(INDICES) + ["all"], default="000300")
    update.add_argument("--metric", choices=list(METRICS) + ["all"], default="pe_ttm")
    diagnose = sub.add_parser("diagnose", help="联网诊断 AKShare 返回结构，不写入数据库")
    diagnose.add_argument("--index", choices=INDICES, default="000300")
    diagnose.add_argument("--metric", choices=list(METRICS) + ["all"], default="all")
    update_market_parser = sub.add_parser("update-market", help="拉取指数自身的日线收盘点位（需akshare）")
    update_market_parser.add_argument("--index", choices=list(INDICES) + ["all"], default="000300")
    update_market_parser.add_argument("--start", default="20160101")
    update_market_parser.add_argument("--end", default=None)
    update_market_parser.add_argument("--provider", choices=["auto", *PROVIDER_CHOICES], default="auto")
    diagnose_market = sub.add_parser("diagnose-market", help="联网诊断指数行情接口，不写入数据库")
    diagnose_market.add_argument("--index", choices=INDICES, default="000300")
    diagnose_market.add_argument("--start", default="20160101")
    diagnose_market.add_argument("--end", default=None)
    diagnose_market.add_argument("--provider", choices=["auto", *PROVIDER_CHOICES], default="auto")
    diagnose_market.add_argument("--cross-check", choices=PROVIDER_CHOICES, default=None)
    import_market = sub.add_parser("import-market", help="从 CSV 受控导入指数收盘点位")
    import_market.add_argument("--file", required=True)
    import_market.add_argument("--index", choices=INDICES, default="000300")
    import_market.add_argument("--source-name", required=True)
    import_market.add_argument("--verification-notes", required=True)
    imp = sub.add_parser("import", help="从CSV导入历史数据")
    imp.add_argument("--file", required=True)
    imp.add_argument("--index", choices=INDICES, default="000300")
    imp.add_argument("--metric", choices=METRICS, default="pe_ttm")
    args = parser.parse_args()
    if args.command == "doctor":
        print(json.dumps({
            **get_runtime_info(),
            "indices": get_coverage_data()["indices"] if DB.is_file() else [],
        }, ensure_ascii=False, indent=2))
        return 0
    elif args.command == "sync":
        try:
            result = sync_all_real_data(
                provider=args.provider, only_missing=args.only_missing, pause_seconds=args.pause
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result["failed"] else 0
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"全量更新未开始：{exc}", file=sys.stderr)
            return 1
    elif args.command == "update":
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
                if len(codes) * len(metrics) > 1:
                    time.sleep(2)
        return 1 if failed else 0
    elif args.command == "update-market":
        failed = 0
        codes = INDICES if args.index == "all" else [args.index]
        for code in codes:
            try:
                print(INDICES[code], json.dumps(update_market(code, args.start, args.end, args.provider), ensure_ascii=False))
            except Exception as exc:
                failed += 1
                print("指数行情更新失败：", INDICES[code], exc, file=sys.stderr)
        return 1 if failed else 0
    elif args.command == "diagnose-market":
        try:
            result = diagnose_index_source(
                args.index, start_date=args.start, end_date=args.end,
                provider_name=args.provider, cross_check_provider=args.cross_check,
            )
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
    elif args.command == "import-market":
        text = Path(args.file).read_text(encoding="utf-8-sig")
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
            normal = {name.strip().lower(): name for name in (reader.fieldnames or []) if name}
            date_column = next((normal.get(name) for name in ("date", "日期", "trade_date") if normal.get(name)), None)
            close_column = next((normal.get(name) for name in ("close", "收盘", "value") if normal.get(name)), None)
            if not date_column or not close_column:
                raise ValueError("指数 CSV 需要 date/日期 与 close/收盘 列")
            rows = [(row.get(date_column), row.get(close_column)) for row in reader]
            result = import_index_history(
                args.index, rows, args.source_name, args.verification_notes, db_path=DB,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"指数 CSV 导入失败：{exc}", file=sys.stderr)
            return 1
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
        info = get_runtime_info()
        print(f"项目目录：{info['project_root']}")
        print(f"当前代码：{info['branch']} @ {info['commit']}")
        print(f"真实数据库：{info['database_path']}（{info['coverage']['available']}/12 序列）")
        print("注意：启动不自动更新行情；先执行 python app.py sync 获取真实数据。")
        print(f"打开浏览器：http://{host}:{port}   （Ctrl+C 结束）")
        try:
            ThreadingHTTPServer((host, port), Handler).serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
