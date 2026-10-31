from __future__ import annotations

from datetime import date
import logging
import multiprocessing
from pathlib import Path
import queue
import time
from typing import Callable

from .history import (
    DEFAULT_DB,
    DataSourceError,
    persist_history,
    read_active_history,
    validate_history,
)
from .storage import record_update_run, utc_now


LOGGER = logging.getLogger("valuation.market")
INDEX_NAMES = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000016": "上证50",
}
MARKET_METRIC = "index_close"
SOURCE_NAME = "东方财富 / AKShare"
SOURCE_INTERFACE = "ak.index_zh_a_hist"
METHODOLOGY = "指数本身的不复权日线收盘点位；不是 ETF 价格，不补齐非交易日，不进行前向填充或插值。"
EXPECTED_COLUMNS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]


def _market_worker(index_code: str, start_date: str, end_date: str, result_queue) -> None:
    try:
        import akshare as ak

        frame = ak.index_zh_a_hist(
            symbol=index_code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
        )
        result_queue.put({
            "ok": True,
            "version": getattr(ak, "__version__", "unknown"),
            "columns": list(frame.columns) if frame is not None else [],
            "records": [] if frame is None else frame.to_dict(orient="records"),
        })
    except Exception as exc:
        result_queue.put({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})


def _call_akshare(index_code: str, start_date: str, end_date: str, timeout_seconds: int) -> dict:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_market_worker,
        args=(index_code, start_date, end_date, result_queue),
        daemon=True,
    )
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise TimeoutError(f"AKShare 指数行情请求超过 {timeout_seconds} 秒，已终止")
    try:
        payload = result_queue.get(timeout=2)
    except queue.Empty as exc:
        raise DataSourceError(f"AKShare 指数行情子进程异常退出（exitcode={process.exitcode}）") from exc
    if not payload.get("ok"):
        raise DataSourceError(f"{payload.get('error_type', 'Error')}: {payload.get('error', '未知错误')}")
    return payload


def fetch_index_history(
    index_code: str,
    *,
    start_date: str = "20160101",
    end_date: str | None = None,
    timeout_seconds: int = 45,
    retries: int = 2,
    provider: Callable[[str, str, str, int], dict] | None = None,
) -> dict:
    if index_code not in INDEX_NAMES:
        raise DataSourceError(f"不支持的指数代码: {index_code}")
    end_date = end_date or date.today().strftime("%Y%m%d")
    caller = provider or _call_akshare
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            payload = caller(index_code, start_date, end_date, timeout_seconds)
            columns = list(payload.get("columns") or [])
            records = list(payload.get("records") or [])
            if not records:
                raise DataSourceError(f"{SOURCE_INTERFACE} 返回空数据")
            missing = sorted({"日期", "收盘"}.difference(columns))
            if missing:
                raise DataSourceError(f"指数行情返回列结构已变化，缺少 {missing}；实际列为 {columns}，已拒绝写库")
            return {
                "index_code": index_code,
                "metric": MARKET_METRIC,
                "rows": [(record.get("日期"), record.get("收盘")) for record in records],
                "fetched_count": len(records),
                "source_version": str(payload.get("version") or "unknown"),
                "columns": columns,
                "source_name": SOURCE_NAME,
                "source_interface": SOURCE_INTERFACE,
                "methodology": METHODOLOGY,
                "series_id": f"eastmoney_akshare_{index_code}_daily_close_v1",
            }
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(attempt * 1.5, 3))
    raise DataSourceError(f"指数行情请求失败（已尝试 {retries} 次）：{last_error}") from last_error


def validate_index_history(rows, *, today: date | None = None) -> dict:
    return validate_history(
        rows,
        today=today,
        maximum_value=10_000_000,
        shock_threshold=.2,
    )


def update_index_history(
    index_code: str,
    *,
    db_path: str | Path = DEFAULT_DB,
    start_date: str = "20160101",
    end_date: str | None = None,
    fetcher: Callable[..., dict] | None = None,
) -> dict:
    started_at = utc_now()
    try:
        if fetcher:
            fetched = fetcher(index_code)
        else:
            fetched = fetch_index_history(index_code, start_date=start_date, end_date=end_date)
        quality = validate_index_history(fetched["rows"])
        return persist_history(db_path, fetched, quality, started_at)
    except Exception as exc:
        LOGGER.exception("index market update failed: %s", index_code)
        try:
            record_update_run(db_path, {
                "index_code": index_code,
                "metric": MARKET_METRIC,
                "started_at": started_at,
                "finished_at": utc_now(),
                "status": "failed",
                "error": str(exc),
            })
        except Exception:
            LOGGER.exception("failed to persist index update failure status")
        raise


def read_index_history(db_path: str | Path, index_code: str):
    return read_active_history(db_path, index_code, MARKET_METRIC)


def diagnose_index_source(
    index_code: str,
    *,
    start_date: str = "20160101",
    end_date: str | None = None,
) -> dict:
    fetched = fetch_index_history(index_code, start_date=start_date, end_date=end_date)
    quality = validate_index_history(fetched["rows"])
    return {
        "akshare_version": fetched["source_version"],
        "columns": fetched["columns"],
        "expected_columns": EXPECTED_COLUMNS,
        "fetched_count": fetched["fetched_count"],
        "source_name": fetched["source_name"],
        "source_interface": fetched["source_interface"],
        "methodology": fetched["methodology"],
        **{key: value for key, value in quality.items() if key != "rows"},
    }
