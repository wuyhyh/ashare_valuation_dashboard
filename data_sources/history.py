from __future__ import annotations

from datetime import date, datetime
from contextlib import closing
import json
import logging
import math
import multiprocessing
from pathlib import Path
import queue
import statistics
import time
from typing import Callable, Iterable

from .storage import connect, json_text, record_update_run, utc_now


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "valuation.sqlite3"
LOGGER = logging.getLogger("valuation.data")
INDEX_NAMES = {
    "000016": "上证50",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
}
SOURCE_SPECS = {
    "pe_ttm": {
        "source_name": "乐咕乐股 / AKShare",
        "source_interface": "ak.stock_index_pe_lg",
        "value_column": "滚动市盈率",
        "series_suffix": "add_ttm_pe_v1",
        "methodology": "AKShare 字段“滚动市盈率”（上游字段 addTtmPe）；非静态、非等权、非中位数口径。上游成分处理细节未独立审计。",
    },
    "pb": {
        "source_name": "乐咕乐股 / AKShare",
        "source_interface": "ak.stock_index_pb_lg",
        "value_column": "市净率",
        "series_suffix": "add_pb_v1",
        "methodology": "AKShare 字段“市净率”（上游字段 addPb）；非等权、非中位数口径。上游成分处理细节未独立审计。",
    },
}


class DataSourceError(RuntimeError):
    pass


class DataValidationError(ValueError):
    pass


def _canonical_date(raw) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    value = str(raw).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别日期: {raw}")


def _akshare_worker(index_name: str, metric: str, result_queue) -> None:
    try:
        import akshare as ak

        function = ak.stock_index_pe_lg if metric == "pe_ttm" else ak.stock_index_pb_lg
        frame = function(symbol=index_name)
        result_queue.put({
            "ok": True,
            "version": getattr(ak, "__version__", "unknown"),
            "columns": list(frame.columns) if frame is not None else [],
            "records": [] if frame is None else frame.to_dict(orient="records"),
        })
    except Exception as exc:
        result_queue.put({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})


def _call_akshare(index_name: str, metric: str, timeout_seconds: int) -> dict:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_akshare_worker, args=(index_name, metric, result_queue), daemon=True)
    process.start()
    try:
        # Drain the pipe BEFORE joining. Joining first can deadlock when an
        # index has thousands of rows and the Queue feeder is still flushing.
        try:
            payload = result_queue.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            if process.is_alive():
                raise TimeoutError(f"AKShare 请求超过 {timeout_seconds} 秒，已终止本次采集") from exc
            raise DataSourceError(f"AKShare 子进程异常退出（exitcode={process.exitcode}）") from exc
    finally:
        process.join(timeout=3)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        result_queue.close()
    if not payload.get("ok"):
        raise DataSourceError(f"{payload.get('error_type', 'Error')}: {payload.get('error', '未知错误')}")
    return payload


def fetch_history(
    index_code: str,
    metric: str,
    *,
    timeout_seconds: int = 35,
    retries: int = 2,
    provider: Callable[[str, str, int], dict] | None = None,
) -> dict:
    if index_code not in INDEX_NAMES:
        raise DataSourceError(f"不支持的指数代码：{index_code}")
    if metric not in SOURCE_SPECS:
        raise DataSourceError(f"不支持的估值指标: {metric}")
    spec = SOURCE_SPECS[metric]
    caller = provider or _call_akshare
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            payload = caller(INDEX_NAMES[index_code], metric, timeout_seconds)
            columns = list(payload.get("columns") or [])
            records = list(payload.get("records") or [])
            if not records:
                raise DataSourceError(f"{spec['source_interface']} 返回空数据")
            required = {"日期", spec["value_column"]}
            missing = sorted(required.difference(columns))
            if missing:
                raise DataSourceError(
                    f"上游返回列结构已变化，缺少 {missing}；实际列为 {columns}。"
                    "已拒绝写库，避免混用静态、等权或中位数口径。"
                )
            upstream_index_values = [
                record.get("指数") for record in records
                if record.get("指数") is not None
            ]
            return {
                "index_code": index_code,
                "metric": metric,
                "rows": [(record.get("日期"), record.get(spec["value_column"])) for record in records],
                "fetched_count": len(records),
                "source_version": str(payload.get("version") or "unknown"),
                "columns": columns,
                "source_name": spec["source_name"],
                "source_interface": spec["source_interface"],
                "methodology": spec["methodology"],
                "series_id": f"legulegu_akshare_{index_code}_{metric}_{spec['series_suffix']}",
                "upstream_index_field": {
                    "column": "指数" if "指数" in columns else None,
                    "kind": "numeric_level" if upstream_index_values else "absent",
                    "sample_count": len(upstream_index_values),
                    "latest_samples": upstream_index_values[-5:],
                    "note": (
                        "上游“指数”列为数值点位，不是指数名称或代码；"
                        "不能单独证明指数身份，需与独立指数收盘点位交叉核验。"
                        if upstream_index_values else "上游未返回可用于辅助核验的指数点位列。"
                    ),
                },
                "upstream_index_rows": [
                    (record.get("日期"), record.get("指数")) for record in records
                    if record.get("指数") is not None
                ],
            }
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                delay = 10 if "429" in str(exc) else min(attempt * 1.5, 3)
                time.sleep(delay)
    raise DataSourceError(f"数据源请求失败（已尝试 {retries} 次）：{last_error}") from last_error


def _frequency(dates: list[date]) -> tuple[str, float | None]:
    if len(dates) < 2:
        return "unknown", None
    gaps = [(right - left).days for left, right in zip(dates, dates[1:])]
    median_gap = float(statistics.median(gaps))
    if median_gap <= 3:
        return "daily", median_gap
    if median_gap <= 10:
        return "weekly", median_gap
    if median_gap <= 45:
        return "monthly", median_gap
    if median_gap <= 100:
        return "quarterly", median_gap
    return "irregular", median_gap


def _gap_summary(dates: list[date], frequency: str) -> tuple[int, int, list[dict]]:
    gaps = []
    missing_periods = 0
    for left, right in zip(dates, dates[1:]):
        days = (right - left).days
        missing = 0
        if frequency == "daily" and days > 7:
            missing = 1
        elif frequency == "weekly" and days > 14:
            missing = max(1, round(days / 7) - 1)
        elif frequency == "monthly":
            month_delta = (right.year - left.year) * 12 + right.month - left.month
            missing = max(0, month_delta - 1)
        elif frequency == "quarterly" and days > 120:
            missing = max(1, round(days / 91) - 1)
        elif frequency in {"irregular", "unknown"} and days > 180:
            missing = 1
        if missing:
            missing_periods += missing
            gaps.append({"from": left.isoformat(), "to": right.isoformat(), "days": days, "missing_periods": missing})
    max_gap = max(((right - left).days for left, right in zip(dates, dates[1:])), default=0)
    return missing_periods, max_gap, gaps[:20]


def _calendar_year_cutoff(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _window_support(dates: list[date]) -> dict[str, bool]:
    if not dates:
        return {str(years): False for years in (3, 5, 8, 10)}
    return {
        str(years): dates[0] <= _calendar_year_cutoff(dates[-1], years) and len(dates) >= 30
        for years in (3, 5, 8, 10)
    }


def validate_history(
    rows: Iterable,
    *,
    today: date | None = None,
    maximum_value: float = 10000,
    shock_threshold: float = .5,
) -> dict:
    today = today or date.today()
    deduplicated: dict[date, float] = {}
    invalid_dates = missing_values = invalid_values = future_dates = duplicate_dates = 0
    for raw in rows:
        raw_date, raw_value = ((raw.get("date"), raw.get("value")) if isinstance(raw, dict) else raw[:2])
        try:
            parsed_date = _canonical_date(raw_date)
        except (TypeError, ValueError):
            invalid_dates += 1
            continue
        if parsed_date > today:
            future_dates += 1
            continue
        if raw_value is None or str(raw_value).strip() in {"", "-", "--", "nan", "NaN", "None"}:
            missing_values += 1
            continue
        try:
            value = float(str(raw_value).replace(",", "").strip())
        except (TypeError, ValueError):
            invalid_values += 1
            continue
        if not math.isfinite(value) or value <= 0 or value >= maximum_value:
            invalid_values += 1
            continue
        if parsed_date in deduplicated:
            duplicate_dates += 1
        deduplicated[parsed_date] = value
    if future_dates:
        raise DataValidationError(f"发现 {future_dates} 条未来日期记录，已拒绝整批写入")
    if not deduplicated:
        raise DataValidationError("没有可写入的有效估值记录")
    dates = sorted(deduplicated)
    frequency, median_gap = _frequency(dates)
    missing_periods, max_gap_days, gaps = _gap_summary(dates, frequency)
    shocks = []
    for left, right in zip(dates, dates[1:]):
        previous, current = deduplicated[left], deduplicated[right]
        change = current / previous - 1
        if abs(change) >= shock_threshold:
            shocks.append({"date": right.isoformat(), "previous": previous, "value": current, "change_pct": round(change * 100, 2)})
    support = _window_support(dates)
    warnings = []
    removed = invalid_dates + missing_values + invalid_values
    if removed:
        warnings.append(f"明确剔除 {removed} 条无效记录（非法日期 {invalid_dates}、缺失值 {missing_values}、非法数值 {invalid_values}）")
    if duplicate_dates:
        warnings.append(f"发现并按日期去重 {duplicate_dates} 条重复记录（保留最后一条）")
    if missing_periods:
        warnings.append(f"按{frequency}频率识别到 {missing_periods} 个明显缺失周期")
    if shocks:
        warnings.append(f"识别到 {len(shocks)} 个超过 {shock_threshold * 100:g}% 的相邻观测突变，已保留并标记")
    unsupported = [f"{years}年" for years, ok in support.items() if not ok]
    if unsupported:
        warnings.append(f"历史覆盖不足以可靠支持：{'、'.join(unsupported)}窗口")
    return {
        "rows": [{"date": item.isoformat(), "value": deduplicated[item]} for item in dates],
        "valid_count": len(dates),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "frequency": frequency,
        "median_gap_days": median_gap,
        "missing_periods": missing_periods,
        "max_gap_days": max_gap_days,
        "gaps": gaps,
        "shock_count": len(shocks),
        "shocks": shocks[:20],
        "invalid_dates": invalid_dates,
        "missing_values": missing_values,
        "invalid_values": invalid_values,
        "duplicate_dates": duplicate_dates,
        "window_support": support,
        "warnings": warnings,
    }


def cross_validate_valuation_identity(
    upstream_index_rows: Iterable,
    market_rows: Iterable,
    *,
    minimum_common_dates: int = 3,
    tolerance_pct: float = .05,
) -> dict:
    """Use upstream numeric index levels only as supporting identity evidence."""
    if not upstream_index_rows:
        return {"status": "unavailable", "reason": "估值源未返回可核验的指数点位列"}
    if not market_rows:
        return {"status": "unavailable", "reason": "本地尚无独立指数收盘序列"}
    upstream = {
        row["date"]: row["value"]
        for row in validate_history(
            upstream_index_rows, maximum_value=10_000_000, shock_threshold=.2,
        )["rows"]
    }
    market = {str(row["date"]): float(row["value"]) for row in market_rows}
    common_dates = sorted(set(upstream).intersection(market))
    samples = []
    for observation_date in common_dates[-5:]:
        valuation_level = upstream[observation_date]
        market_close = market[observation_date]
        difference_pct = 100 * (valuation_level - market_close) / market_close if market_close else None
        samples.append({
            "date": observation_date,
            "valuation_index_level": valuation_level,
            "market_close": market_close,
            "difference_pct": round(difference_pct, 6) if difference_pct is not None else None,
        })
    if len(common_dates) < minimum_common_dates:
        return {
            "status": "unavailable", "common_count": len(common_dates), "samples": samples,
            "reason": f"估值源与独立点位共同日期仅 {len(common_dates)} 个",
        }
    maximum_difference = max(
        abs(item["difference_pct"]) for item in samples if item["difference_pct"] is not None
    )
    return {
        "status": "passed" if maximum_difference <= tolerance_pct else "warning",
        "common_count": len(common_dates), "sample_count": len(samples),
        "max_abs_difference_pct": round(maximum_difference, 6), "samples": samples,
        "note": "该核验仅证明乐咕数值“指数”列与独立指数收盘吻合；估值计算口径仍以字段和来源说明为准。",
    }


def _insert_run(connection, record: dict) -> None:
    connection.execute("""
        INSERT INTO update_runs(index_code,metric,series_id,started_at,finished_at,status,
            fetched_count,valid_count,first_date,last_date,source_version,columns_json,
            warnings_json,error)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        record["index_code"], record["metric"], record.get("series_id"), record["started_at"],
        record["finished_at"], record["status"], record.get("fetched_count", 0),
        record.get("valid_count", 0), record.get("first_date"), record.get("last_date"),
        record.get("source_version"), json_text(record.get("columns", [])),
        json_text(record.get("warnings", [])), record.get("error"),
    ))


def persist_history(db_path: str | Path, fetched: dict, quality: dict, started_at: str) -> dict:
    now = utc_now()
    with closing(connect(db_path)) as connection, connection:
        existing = {
            row["date"]: row["value"]
            for row in connection.execute("SELECT date,value FROM observations WHERE series_id=?", (fetched["series_id"],))
        }
        inserted = sum(row["date"] not in existing for row in quality["rows"])
        changed = sum(row["date"] in existing and not math.isclose(existing[row["date"]], row["value"], rel_tol=0, abs_tol=1e-12)
                      for row in quality["rows"])
        connection.execute("""
            INSERT INTO series(series_id,index_code,metric,source_name,source_interface,methodology,
                               frequency,quality_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(series_id) DO UPDATE SET
                source_name=excluded.source_name,source_interface=excluded.source_interface,
                methodology=excluded.methodology,frequency=excluded.frequency,
                quality_json=excluded.quality_json,updated_at=excluded.updated_at
        """, (
            fetched["series_id"], fetched["index_code"], fetched["metric"], fetched["source_name"],
            fetched["source_interface"], fetched["methodology"], quality["frequency"],
            json_text({key: value for key, value in quality.items() if key != "rows"}), now, now,
        ))
        connection.executemany("""
            INSERT INTO observations(series_id,index_code,metric,date,value,source,fetched_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(series_id,date) DO UPDATE SET
                value=excluded.value,source=excluded.source,fetched_at=excluded.fetched_at
        """, [
            (fetched["series_id"], fetched["index_code"], fetched["metric"], row["date"], row["value"],
             fetched["source_name"], now) for row in quality["rows"]
        ])
        connection.execute("""
            INSERT INTO active_series(index_code,metric,series_id) VALUES(?,?,?)
            ON CONFLICT(index_code,metric) DO UPDATE SET series_id=excluded.series_id
        """, (fetched["index_code"], fetched["metric"], fetched["series_id"]))
        run = {
            "index_code": fetched["index_code"], "metric": fetched["metric"],
            "series_id": fetched["series_id"], "started_at": started_at, "finished_at": now,
            "status": "success", "fetched_count": fetched.get("fetched_count", len(quality["rows"])),
            "valid_count": quality["valid_count"], "first_date": quality["first_date"],
            "last_date": quality["last_date"], "source_version": fetched.get("source_version"),
            "columns": fetched.get("columns", []), "warnings": quality["warnings"], "error": None,
        }
        _insert_run(connection, run)
    return {
        "count": quality["valid_count"], "inserted": inserted, "updated": changed,
        "unchanged": quality["valid_count"] - inserted - changed,
        "first_date": quality["first_date"], "last_date": quality["last_date"],
        "source": fetched["source_name"], "source_interface": fetched["source_interface"],
        "methodology": fetched["methodology"], "frequency": quality["frequency"],
        "warnings": quality["warnings"], "series_id": fetched["series_id"],
    }


def update_history(
    index_code: str,
    metric: str,
    *,
    db_path: str | Path = DEFAULT_DB,
    fetcher: Callable[[str, str], dict] | None = None,
) -> dict:
    started_at = utc_now()
    try:
        fetched = (fetcher or fetch_history)(index_code, metric)
        quality = validate_history(fetched["rows"])
        if fetched.get("upstream_index_field"):
            quality["upstream_index_field"] = fetched["upstream_index_field"]
        if fetched.get("upstream_index_rows"):
            market_rows, market_status = read_active_history(db_path, index_code, "index_close")
            quality["source_identity"] = {
                **cross_validate_valuation_identity(fetched["upstream_index_rows"], market_rows),
                "market_source_interface": (market_status or {}).get("source_interface"),
                "market_series_id": (market_status or {}).get("series_id"),
            }
        return persist_history(db_path, fetched, quality, started_at)
    except Exception as exc:
        LOGGER.exception("valuation update failed: %s %s", index_code, metric)
        try:
            record_update_run(db_path, {
                "index_code": index_code, "metric": metric, "started_at": started_at,
                "finished_at": utc_now(), "status": "failed", "error": str(exc),
            })
        except Exception:
            LOGGER.exception("failed to persist update failure status")
        raise


def import_history(
    index_code: str,
    metric: str,
    rows: Iterable,
    source_name: str,
    *,
    db_path: str | Path = DEFAULT_DB,
) -> dict:
    started_at = utc_now()
    quality = validate_history(rows)
    fetched = {
        "index_code": index_code, "metric": metric,
        "series_id": f"user_csv_{index_code}_{metric}_declared_same_method_v1",
        "source_name": source_name, "source_interface": "local.csv",
        "methodology": "用户 CSV 声明为同一估值口径；系统不与 AKShare 序列拼接，口径需用户自行复核",
        "source_version": "user-import", "columns": ["date", "value"],
        "fetched_count": quality["valid_count"],
    }
    return persist_history(db_path, fetched, quality, started_at)


def read_active_history(db_path: str | Path, index_code: str, metric: str) -> tuple[list[dict], dict | None]:
    with closing(connect(db_path)) as connection, connection:
        active = connection.execute("""
            SELECT s.* FROM active_series a JOIN series s ON s.series_id=a.series_id
            WHERE a.index_code=? AND a.metric=?
        """, (index_code, metric)).fetchone()
        if active is None:
            return [], _latest_failure(connection, index_code, metric)
        rows = connection.execute("""
            SELECT date,value,source,fetched_at FROM observations
            WHERE series_id=? ORDER BY date
        """, (active["series_id"],)).fetchall()
        status = _status_from_rows(connection, active, rows)
    return [dict(row) for row in rows], status


def _latest_failure(connection, index_code: str, metric: str) -> dict | None:
    run = connection.execute("""
        SELECT * FROM update_runs WHERE index_code=? AND metric=? ORDER BY id DESC LIMIT 1
    """, (index_code, metric)).fetchone()
    if run is None:
        return None
    return {
        "data_state": "update_failed" if run["status"] == "failed" else "no_data",
        "last_update_status": run["status"], "last_error": run["error"],
        "last_attempt_at": run["finished_at"], "count": 0,
    }


def _status_from_rows(connection, series, rows) -> dict:
    quality = json.loads(series["quality_json"] or "{}")
    latest_run = connection.execute("""
        SELECT * FROM update_runs WHERE index_code=? AND metric=? ORDER BY id DESC LIMIT 1
    """, (series["index_code"], series["metric"])).fetchone()
    first_date = rows[0]["date"] if rows else None
    last_date = rows[-1]["date"] if rows else None
    fetched_at = max((row["fetched_at"] for row in rows), default=None)
    stale_days = {"daily": 10, "weekly": 21, "monthly": 45, "quarterly": 120}.get(series["frequency"], 45)
    is_stale = bool(last_date and (date.today() - date.fromisoformat(last_date)).days > stale_days)
    failed = bool(latest_run and latest_run["status"] == "failed")
    stored_support = quality.get("window_support", {})
    row_dates = [date.fromisoformat(row["date"]) for row in rows]
    support = {**_window_support(row_dates), **stored_support}
    if "10" not in stored_support:
        support["10"] = _window_support(row_dates)["10"]
    insufficient = not all(support.get(str(years), False) for years in (3, 5, 8, 10))
    data_state = "update_failed" if failed else "stale" if is_stale else "insufficient" if insufficient else "ready"
    return {
        "data_state": data_state, "series_id": series["series_id"],
        "source_name": series["source_name"], "source_interface": series["source_interface"],
        "methodology": series["methodology"], "frequency": series["frequency"],
        "count": len(rows), "first_date": first_date, "last_date": last_date,
        "fetched_at": fetched_at, "has_gaps": bool(quality.get("missing_periods")),
        "missing_periods": quality.get("missing_periods", 0),
        "max_gap_days": quality.get("max_gap_days", 0),
        "shock_count": quality.get("shock_count", 0),
        "cross_validation": quality.get("cross_validation"),
        "source_identity": quality.get("source_identity"),
        "window_support": support, "warnings": quality.get("warnings", []),
        "is_stale": is_stale,
        "last_update_status": latest_run["status"] if latest_run else None,
        "last_error": latest_run["error"] if latest_run else None,
        "last_attempt_at": latest_run["finished_at"] if latest_run else None,
    }


def get_update_status(
    *,
    db_path: str | Path = DEFAULT_DB,
    index_code: str | None = None,
    metric: str | None = None,
) -> dict:
    result = {}
    with closing(connect(db_path)) as connection, connection:
        query = "SELECT s.* FROM active_series a JOIN series s ON s.series_id=a.series_id"
        params = []
        clauses = []
        if index_code:
            clauses.append("a.index_code=?")
            params.append(index_code)
        if metric:
            clauses.append("a.metric=?")
            params.append(metric)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        for series in connection.execute(query, params):
            rows = connection.execute("SELECT date,value,source,fetched_at FROM observations WHERE series_id=? ORDER BY date",
                                      (series["series_id"],)).fetchall()
            result[f"{series['index_code']}:{series['metric']}"] = _status_from_rows(connection, series, rows)
        run_query = "SELECT * FROM update_runs ORDER BY id DESC"
        for run in connection.execute(run_query):
            key = f"{run['index_code']}:{run['metric']}"
            if (index_code and run["index_code"] != index_code) or (metric and run["metric"] != metric):
                continue
            if key not in result:
                result[key] = _latest_failure(connection, run["index_code"], run["metric"])
    return result


def diagnose_source(index_code: str, metric: str) -> dict:
    fetched = fetch_history(index_code, metric)
    quality = validate_history(fetched["rows"])
    rows = quality["rows"]
    return {
        "index_code": index_code, "index_name": INDEX_NAMES[index_code],
        "akshare_version": fetched["source_version"], "columns": fetched["columns"],
        "fetched_count": fetched["fetched_count"], "expected_column": SOURCE_SPECS[metric]["value_column"],
        "source_interface": fetched["source_interface"], "methodology": fetched["methodology"],
        "first_value": rows[0]["value"], "last_value": rows[-1]["value"],
        "latest_samples": rows[-5:],
        "upstream_index_field": fetched.get("upstream_index_field"),
        **{key: value for key, value in quality.items() if key != "rows"},
    }
