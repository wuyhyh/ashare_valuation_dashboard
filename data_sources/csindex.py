"""Explicit opt-in CSI Index official historical close / PE TTM import.

No implicit fallback, no source concatenation, no database mutation during diagnostics.
The upstream endpoint is subject to availability and must be verified on a real network.
"""
from __future__ import annotations

from datetime import date, datetime
import multiprocessing
import queue
from pathlib import Path
from typing import Callable

from .history import DEFAULT_DB, DataSourceError, persist_history, validate_history
from .storage import record_update_run, utc_now


INDEX_CODES = {"000300", "000905", "000852", "000016"}
SOURCE_NAME = "中证指数有限公司 / AKShare"
SOURCE_INTERFACE = "ak.stock_zh_index_hist_csindex"
METRIC_FIELDS = {"index_close": "收盘", "pe_ttm": "滚动市盈率"}
METHODOLOGIES = {
    "index_close": "中证指数官网披露的指数收盘点位；非ETF价格；不插值、不前向填充。",
    "pe_ttm": "中证指数官网披露的滚动市盈率字段；未独立审计其成分处理与利润计算细节；不可与乐咕乐股PE序列拼接。",
}


def _ak_worker(index_code: str, start_date: str, end_date: str, pipe) -> None:
    try:
        import akshare as ak
        frame = ak.stock_zh_index_hist_csindex(
            symbol=index_code, start_date=start_date, end_date=end_date
        )
        pipe.send({
            "ok": True,
            "version": getattr(ak, "__version__", "unknown"),
            "columns": list(frame.columns) if frame is not None else [],
            "records": [] if frame is None else frame.to_dict(orient="records"),
        })
    except Exception as exc:
        pipe.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        pipe.close()


def _call_akshare(index_code: str, start_date: str, end_date: str, timeout: int) -> dict:
    # Pipe polling BEFORE joining avoids a large-data Queue feeder deadlock.
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_ak_worker, args=(index_code, start_date, end_date, sender), daemon=True
    )
    try:
        proc.start()
        sender.close()
        if not receiver.poll(timeout):
            raise DataSourceError(f"官方行情请求超时（{timeout}秒）或采集进程异常退出")
        try:
            payload = receiver.recv()
        except EOFError as exc:
            raise DataSourceError("官方行情采集进程提前退出") from exc
        if not payload.get("ok"):
            raise DataSourceError(payload.get("error") or "未知采集错误")
        return payload
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=3)
        receiver.close()
        sender.close()


def _check_date_arg(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"{label}需要YYYYMMDD格式") from exc


def _record_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    raise DataSourceError(f"官方接口返回无法识别的日期: {value}")


def fetch_official(
    index_code: str,
    *,
    start_date: str = "20160101",
    end_date: str | None = None,
    timeout: int = 60,
    provider: Callable[[str, str, str, int], dict] | None = None,
) -> dict:
    """Fetch a single raw response containing close and (where present) TTM PE."""
    if index_code not in INDEX_CODES:
        raise DataSourceError(f"不支持的指数: {index_code}")
    end_date = end_date or date.today().strftime("%Y%m%d")
    start, end = _check_date_arg(start_date, "起始日期"), _check_date_arg(end_date, "截止日期")
    if start > end or end > date.today():
        raise ValueError("日期范围无效或包含未来日期")
    if timeout <= 0:
        raise ValueError("timeout必须为正数")
    padded_start = (start - date.resolution).strftime("%Y%m%d")
    payload = (provider or _call_akshare)(index_code, padded_start, end_date, timeout)
    columns, records = list(payload.get("columns") or []), list(payload.get("records") or [])
    if not records:
        raise DataSourceError("中证指数接口返回空数据，未写库")
    if "日期" not in columns or "收盘" not in columns:
        raise DataSourceError(f"官方接口缺少日期/收盘列，实际字段: {columns}")
    if "指数代码" not in columns:
        raise DataSourceError("官方接口缺少指数代码列，无法证明返回的是请求的指数")
    records = [record for record in records if start <= _record_date(record.get("日期")) <= end]
    if not records:
        raise DataSourceError("中证指数接口在请求区间内没有有效记录，未写库")
    received = set()
    for record in records:
        raw_code = str(record.get("指数代码") or "").strip()
        raw_code = raw_code.split(".")[0].zfill(6) if raw_code.isdigit() else raw_code
        received.add(raw_code)
    if received != {index_code}:
        raise DataSourceError(f"指数代码不匹配: 请求{index_code}, 返回{sorted(received)}")
    return {"index_code": index_code, "records": records, "columns": columns,
            "version": str(payload.get("version") or "unknown"),
            "requested_start": start.isoformat(), "requested_end": end.isoformat()}


def _prepare(raw: dict, metric: str) -> tuple[dict, dict]:
    if metric not in METRIC_FIELDS:
        raise ValueError(f"不支持的指标：{metric}")
    field = METRIC_FIELDS[metric]
    if field not in raw["columns"]:
        raise DataSourceError(f"官方接口缺少“{field}”字段；该指标未写库")
    index_code = raw["index_code"]
    fetched = {
        "index_code": index_code, "metric": metric,
        "rows": [(row.get("日期"), row.get(field)) for row in raw["records"]],
        "fetched_count": len(raw["records"]), "source_version": raw["version"],
        "columns": raw["columns"], "source_name": SOURCE_NAME,
        "source_interface": SOURCE_INTERFACE,
        "methodology": METHODOLOGIES[metric],
        "series_id": f"csindex_official_{index_code}_{metric}_v1",
    }
    quality = validate_history(
        fetched["rows"], maximum_value=10_000_000 if metric == "index_close" else 10000,
        shock_threshold=.2 if metric == "index_close" else .5,
    )
    if quality["valid_count"] < 10:
        raise DataSourceError(f"官方{metric}有效样本仅{quality['valid_count']}条，不足10条；未写库")
    return fetched, quality


def diagnose_official(
    index_code: str, *, start_date: str = "20160101", end_date: str | None = None,
    metric: str = "all", provider=None,
) -> dict:
    raw = fetch_official(index_code, start_date=start_date, end_date=end_date, provider=provider)
    result = {"index": index_code, "source": SOURCE_INTERFACE, "version": raw["version"],
              "columns": raw["columns"], "requested_start": raw["requested_start"],
              "requested_end": raw["requested_end"], "metrics": {}}
    for selected in (METRIC_FIELDS if metric == "all" else [metric]):
        try:
            _, quality = _prepare(raw, selected)
            rows = quality["rows"]
            result["metrics"][selected] = {
                **{k: v for k, v in quality.items() if k != "rows"},
                "first_value": rows[0]["value"], "last_value": rows[-1]["value"],
                "latest_samples": rows[-5:],
            }
        except (DataSourceError, ValueError) as exc:
            result["metrics"][selected] = {"error": str(exc)}
    return result


def update_official(
    index_code: str, *, metric: str = "all", db_path: str | Path = DEFAULT_DB,
    start_date: str = "20160101", end_date: str | None = None, provider=None,
) -> dict:
    """All requested series validated before any write; existing series never spliced."""
    selected = list(METRIC_FIELDS) if metric == "all" else [metric]
    if any(item not in METRIC_FIELDS for item in selected):
        raise ValueError("官方源仅支持index_close和pe_ttm")
    started = utc_now()
    try:
        raw = fetch_official(index_code, start_date=start_date, end_date=end_date, provider=provider)
        prepared = [_prepare(raw, item) for item in selected]
        return {fetched["metric"]: persist_history(db_path, fetched, quality, started)
                for fetched, quality in prepared}
    except Exception as exc:
        # Persist a failed attempt without modifying active observations.
        for item in selected:
            try:
                record_update_run(db_path, {
                    "index_code": index_code, "metric": item,
                    "started_at": started, "finished_at": utc_now(),
                    "status": "failed", "error": str(exc),
                })
            except Exception:
                pass
        raise


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="中证指数官方历史行情/PE 诊断与显式导入")
    parser.add_argument("command", choices=["diagnose", "update"])
    parser.add_argument("--index", choices=sorted(INDEX_CODES), default="000300")
    parser.add_argument("--metric", choices=["index_close", "pe_ttm", "all"], default="all")
    parser.add_argument("--start", default="20160101")
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose":
            result = diagnose_official(args.index, start_date=args.start,
                                       end_date=args.end, metric=args.metric)
        else:
            result = update_official(args.index, start_date=args.start,
                                     end_date=args.end, metric=args.metric)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"官方数据{args.command}失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
