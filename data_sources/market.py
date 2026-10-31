from __future__ import annotations

from datetime import date, datetime
import hashlib
import logging
import multiprocessing
from pathlib import Path
import queue
import time
from typing import Callable, Iterable

from .history import DEFAULT_DB, DataSourceError, persist_history, read_active_history, validate_history
from .storage import record_update_run, utc_now


LOGGER = logging.getLogger("valuation.market")
INDEX_NAMES = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000", "000016": "上证50"}
MARKET_METRIC = "index_close"
METHODOLOGY = "指数本身的不复权日线收盘点位；不是 ETF 价格，不补齐非交易日，不进行前向填充或插值。"
PROVIDER_ORDER = ("sina", "tencent", "eastmoney_daily", "eastmoney_legacy")
PROVIDER_CHOICES = (*PROVIDER_ORDER, "csindex_official")
PROVIDER_SPECS = {
    "sina": {
        "source_name": "新浪财经 / AKShare", "source_interface": "ak.stock_zh_index_daily",
        "methodology": METHODOLOGY + " 新浪接口返回完整历史，本地按请求日期裁剪。",
        "symbol": lambda code: f"sh{code}", "date_column": "date", "close_column": "close",
    },
    "tencent": {
        "source_name": "腾讯财经 / AKShare", "source_interface": "ak.stock_zh_index_daily_tx",
        "methodology": METHODOLOGY + " 腾讯接口按日期请求；未将其文档中的复权描述解释为 ETF 复权。",
        "symbol": lambda code: f"sh{code}", "date_column": "date", "close_column": "close",
    },
    "eastmoney_daily": {
        "source_name": "东方财富 / AKShare", "source_interface": "ak.stock_zh_index_daily_em",
        "methodology": METHODOLOGY + " 东方财富指数日线接口。",
        "symbol": lambda code: f"sh{code}" if code == "000016" else f"csi{code}",
        "date_column": "date", "close_column": "close",
    },
    "eastmoney_legacy": {
        "source_name": "东方财富 / AKShare", "source_interface": "ak.index_zh_a_hist",
        "methodology": METHODOLOGY + " 东方财富 A 股指数历史接口。",
        "symbol": lambda code: code, "date_column": "日期", "close_column": "收盘",
    },
    "csindex_official": {
        "source_name": "中证指数有限公司 / AKShare", "source_interface": "ak.stock_zh_index_hist_csindex",
        "methodology": METHODOLOGY + " 中证指数官网披露的指数收盘点位；作为显式可选来源，不参与 auto 回退。",
        "symbol": lambda code: code, "date_column": "日期", "close_column": "收盘",
    },
}
SOURCE_NAME = PROVIDER_SPECS["eastmoney_legacy"]["source_name"]
SOURCE_INTERFACE = PROVIDER_SPECS["eastmoney_legacy"]["source_interface"]
EXPECTED_COLUMNS = ["日期", "收盘"]


def _provider_worker(provider_name: str, index_code: str, start_date: str, end_date: str, result_queue) -> None:
    try:
        import akshare as ak

        spec = PROVIDER_SPECS[provider_name]
        symbol = spec["symbol"](index_code)
        if provider_name == "sina":
            frame = ak.stock_zh_index_daily(symbol=symbol)
        elif provider_name == "tencent":
            frame = ak.stock_zh_index_daily_tx(symbol=symbol, start_date=start_date, end_date=end_date)
        elif provider_name == "eastmoney_daily":
            frame = ak.stock_zh_index_daily_em(symbol=symbol, start_date=start_date, end_date=end_date)
        else:
            frame = ak.index_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date)
        result_queue.put({
            "ok": True, "version": getattr(ak, "__version__", "unknown"),
            "columns": list(frame.columns) if frame is not None else [],
            "records": [] if frame is None else frame.to_dict(orient="records"),
        })
    except Exception as exc:
        result_queue.put({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})


def _call_provider(provider_name: str, index_code: str, start_date: str, end_date: str, timeout_seconds: int) -> dict:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_provider_worker, args=(provider_name, index_code, start_date, end_date, result_queue), daemon=True,
    )
    process.start()
    try:
        payload = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        process.terminate()
        process.join(5)
        raise TimeoutError(f"{PROVIDER_SPECS[provider_name]['source_interface']} 请求超过 {timeout_seconds} 秒，已终止") from exc
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if not payload.get("ok"):
        raise DataSourceError(f"{payload.get('error_type', 'Error')}: {payload.get('error', '未知错误')}")
    return payload


def _call_akshare(index_code: str, start_date: str, end_date: str, timeout_seconds: int) -> dict:
    return _call_provider("eastmoney_legacy", index_code, start_date, end_date, timeout_seconds)


def _parse_date(raw) -> str:
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    value = str(raw).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


def _normalize_payload(index_code: str, provider_name: str, payload: dict, start_date: str, end_date: str) -> dict:
    spec = PROVIDER_SPECS[provider_name]
    columns = list(payload.get("columns") or [])
    records = list(payload.get("records") or [])
    if not records:
        raise DataSourceError(f"{spec['source_interface']} 返回空数据")
    missing = sorted({spec["date_column"], spec["close_column"]}.difference(columns))
    if missing:
        raise DataSourceError(f"指数行情返回列结构已变化，缺少 {missing}；实际列为 {columns}，已拒绝写库")
    start_iso, end_iso = _parse_date(start_date), _parse_date(end_date)
    rows = []
    for record in records:
        observation_date = _parse_date(record.get(spec["date_column"]))
        if start_iso <= observation_date <= end_iso:
            rows.append((observation_date, record.get(spec["close_column"])))
    if not rows:
        raise DataSourceError(f"{spec['source_interface']} 在请求区间内没有数据")
    return {
        "index_code": index_code, "metric": MARKET_METRIC, "rows": rows,
        "fetched_count": len(rows), "source_version": str(payload.get("version") or "unknown"),
        "columns": columns, "source_name": spec["source_name"], "source_interface": spec["source_interface"],
        "methodology": spec["methodology"], "series_id": f"{provider_name}_akshare_{index_code}_daily_close_v1",
        "provider": provider_name,
    }


def fetch_index_history(
    index_code: str, *, start_date: str = "20160101", end_date: str | None = None,
    timeout_seconds: int = 45, retries: int = 2, provider_name: str = "auto",
    provider: Callable[[str, str, str, int], dict] | None = None,
) -> dict:
    if index_code not in INDEX_NAMES:
        raise DataSourceError(f"不支持的指数代码: {index_code}")
    if provider_name not in ("auto", *PROVIDER_CHOICES):
        raise DataSourceError(f"不支持的指数行情数据源: {provider_name}")
    end_date = end_date or date.today().strftime("%Y%m%d")
    if provider is not None:
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                return _normalize_payload(index_code, "eastmoney_legacy", provider(index_code, start_date, end_date, timeout_seconds), start_date, end_date)
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(min(attempt * 1.5, 3))
        raise DataSourceError(f"指数行情请求失败（已尝试 {retries} 次）：{last_error}") from last_error

    candidates = PROVIDER_ORDER if provider_name == "auto" else (provider_name,)
    errors = []
    for candidate in candidates:
        attempts = 1 if provider_name == "auto" else retries
        for attempt in range(1, attempts + 1):
            try:
                if candidate == "csindex_official":
                    from .csindex import fetch_official
                    official = fetch_official(
                        index_code, start_date=start_date, end_date=end_date,
                        timeout=timeout_seconds,
                    )
                    payload = {
                        "version": official["version"], "columns": official["columns"],
                        "records": official["records"],
                    }
                else:
                    payload = _call_provider(candidate, index_code, start_date, end_date, timeout_seconds)
                return _normalize_payload(index_code, candidate, payload, start_date, end_date)
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
                if attempt < attempts:
                    time.sleep(min(attempt * 1.5, 3))
        if provider_name == "auto" and candidate != candidates[-1]:
            time.sleep(.5)
    raise DataSourceError("指数行情请求失败：" + "；".join(errors))


def validate_index_history(rows, *, today: date | None = None) -> dict:
    return validate_history(rows, today=today, maximum_value=10_000_000, shock_threshold=.2)


def cross_validate_sources(primary: dict, secondary: dict, *, minimum_common_days: int = 5) -> dict:
    primary_rows = {row["date"]: row["value"] for row in validate_index_history(primary["rows"])["rows"]}
    secondary_rows = {row["date"]: row["value"] for row in validate_index_history(secondary["rows"])["rows"]}
    common_dates = sorted(set(primary_rows).intersection(secondary_rows))
    samples = []
    for observation_date in common_dates[-minimum_common_days:]:
        left, right = primary_rows[observation_date], secondary_rows[observation_date]
        samples.append({
            "date": observation_date, "primary": left, "secondary": right,
            "difference": round(left - right, 6),
            "difference_pct": round(100 * (left - right) / right, 6) if right else None,
        })
    if len(common_dates) < minimum_common_days:
        return {"status": "unavailable", "reason": f"共同交易日仅 {len(common_dates)} 个，少于 {minimum_common_days} 个", "common_count": len(common_dates), "samples": samples}
    maximum_difference_pct = max(abs(item["difference_pct"]) for item in samples if item["difference_pct"] is not None)
    return {
        "status": "passed" if maximum_difference_pct <= .05 else "warning",
        "primary_interface": primary["source_interface"], "secondary_interface": secondary["source_interface"],
        "common_count": len(common_dates), "sample_count": len(samples),
        "max_abs_difference_pct": round(maximum_difference_pct, 6), "samples": samples,
    }


def _independent_provider(provider_name: str) -> str | None:
    return next((candidate for candidate in PROVIDER_ORDER if candidate != provider_name), None)


def _ensure_candidate_preserves_history(previous_rows: list[dict], quality: dict) -> None:
    if not previous_rows:
        return
    previous_first = date.fromisoformat(previous_rows[0]["date"])
    previous_last = date.fromisoformat(previous_rows[-1]["date"])
    candidate_first = date.fromisoformat(quality["first_date"])
    candidate_last = date.fromisoformat(quality["last_date"])
    previous_span = max((previous_last - previous_first).days, 1)
    candidate_span = max((candidate_last - candidate_first).days, 0)
    reasons = []
    if candidate_first > previous_first + date.resolution * 45:
        reasons.append(f"候选起始日 {candidate_first} 晚于原历史 {previous_first}")
    if candidate_last < previous_last - date.resolution * 15:
        reasons.append(f"候选截止日 {candidate_last} 早于原历史 {previous_last}")
    if candidate_span < previous_span * .8:
        reasons.append(f"候选覆盖跨度仅为原历史的 {candidate_span / previous_span:.1%}")
    if quality["valid_count"] < len(previous_rows) * .5:
        reasons.append(f"候选记录仅 {quality['valid_count']} 条，原历史为 {len(previous_rows)} 条")
    if reasons:
        raise DataSourceError("候选行情历史不足，已保留原活动序列：" + "；".join(reasons))


def update_index_history(
    index_code: str, *, db_path: str | Path = DEFAULT_DB, start_date: str = "20160101",
    end_date: str | None = None, provider_name: str = "auto", fetcher: Callable[..., dict] | None = None,
) -> dict:
    started_at = utc_now()
    try:
        previous_rows, previous_status = read_index_history(db_path, index_code)
        fetched = fetcher(index_code) if fetcher else fetch_index_history(
            index_code, start_date=start_date, end_date=end_date, provider_name=provider_name,
        )
        quality = validate_index_history(fetched["rows"])
        series_changed = bool(
            previous_rows and previous_status
            and previous_status.get("series_id") != fetched.get("series_id")
        )
        if series_changed:
            _ensure_candidate_preserves_history(previous_rows, quality)
        source_changed = bool(previous_rows and previous_status and previous_status.get("source_interface") != fetched.get("source_interface"))
        if not fetcher and (not previous_rows or source_changed):
            secondary_name = _independent_provider(fetched.get("provider", "sina"))
            if secondary_name:
                try:
                    recent_start = max(start_date, quality["last_date"].replace("-", "")[:6] + "01")
                    secondary = fetch_index_history(index_code, start_date=recent_start, end_date=end_date, timeout_seconds=30, retries=1, provider_name=secondary_name)
                    quality["cross_validation"] = cross_validate_sources(fetched, secondary)
                except Exception as exc:
                    quality["cross_validation"] = {"status": "unavailable", "secondary_provider": secondary_name, "reason": str(exc)}
        return persist_history(db_path, fetched, quality, started_at)
    except Exception as exc:
        LOGGER.exception("index market update failed: %s", index_code)
        try:
            record_update_run(db_path, {"index_code": index_code, "metric": MARKET_METRIC, "started_at": started_at, "finished_at": utc_now(), "status": "failed", "error": str(exc)})
        except Exception:
            LOGGER.exception("failed to persist index update failure status")
        raise


def import_index_history(
    index_code: str, rows: Iterable, source_name: str, verification_notes: str, *, db_path: str | Path = DEFAULT_DB,
) -> dict:
    if index_code not in INDEX_NAMES:
        raise DataSourceError(f"不支持的指数代码: {index_code}")
    if not source_name.strip() or not verification_notes.strip():
        raise DataSourceError("CSV 指数导入必须填写来源名称和核验说明")
    quality = validate_index_history(rows)
    identity = hashlib.sha1(f"{source_name}|{verification_notes}".encode("utf-8")).hexdigest()[:12]
    fetched = {
        "index_code": index_code, "metric": MARKET_METRIC, "rows": quality["rows"],
        "fetched_count": quality["valid_count"], "source_version": "user-import", "columns": ["date", "close"],
        "source_name": f"用户 CSV：{source_name.strip()}", "source_interface": "local.csv.index_close",
        "methodology": METHODOLOGY + f" 用户核验说明：{verification_notes.strip()}；不标记为官方数据。",
        "series_id": f"user_csv_{index_code}_index_close_{identity}",
    }
    return persist_history(db_path, fetched, quality, utc_now())


def read_index_history(db_path: str | Path, index_code: str):
    return read_active_history(db_path, index_code, MARKET_METRIC)


def diagnose_index_source(
    index_code: str, *, start_date: str = "20160101", end_date: str | None = None,
    provider_name: str = "auto", cross_check_provider: str | None = None,
) -> dict:
    fetched = fetch_index_history(index_code, start_date=start_date, end_date=end_date, retries=1, provider_name=provider_name)
    quality = validate_index_history(fetched["rows"])
    result = {
        "provider": fetched.get("provider"), "akshare_version": fetched["source_version"],
        "columns": fetched["columns"], "fetched_count": fetched["fetched_count"],
        "source_name": fetched["source_name"], "source_interface": fetched["source_interface"],
        "methodology": fetched["methodology"], **{key: value for key, value in quality.items() if key != "rows"},
    }
    if cross_check_provider:
        secondary = fetch_index_history(index_code, start_date=start_date, end_date=end_date, retries=1, provider_name=cross_check_provider)
        result["cross_validation"] = cross_validate_sources(fetched, secondary)
    return result
