"""Independent price-only research series for indices and gold ETF.

This module does not calculate P/E or P/B, and never substitutes ETF prices
for an index level or gold spot benchmark.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
import json
import math
import multiprocessing
from pathlib import Path
import queue
import sqlite3
from statistics import mean
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "asset_prices.sqlite3"
TZ = ZoneInfo("Asia/Shanghai")
ASSETS = {
    "000688": {
        "name": "科创50", "symbol": "sh000688", "type": "equity_index",
        "unit": "点", "since": "20191231", "source_interface": "ak.stock_zh_index_daily",
        "source_name": "新浪指数日线 / AKShare", "method": "未复权的指数收盘点位",
    },
    "399673": {
        "name": "创业板50", "symbol": "sz399673", "type": "equity_index",
        "unit": "点", "since": "20140101", "source_interface": "ak.stock_zh_index_daily",
        "source_name": "新浪指数日线 / AKShare", "method": "未复权的指数收盘点位",
    },
    "518880": {
        "name": "华安黄金ETF", "symbol": "518880", "type": "gold_etf",
        "unit": "元/份", "since": "20130718", "source_interface": "ak.fund_etf_hist_em",
        "source_name": "东方财富ETF历史行情 / AKShare",
        "method": "场内未复权收盘价；不是Au99.99、ETF净值或复权总回报",
    },
}


class AssetDataError(ValueError):
    pass


def _canonical_date(raw) -> str:
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    v = str(raw).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    raise AssetDataError("无法解析行情日期: " + str(raw))


def _worker(asset_id: str, result_queue):
    try:
        import akshare as ak
        spec = ASSETS[asset_id]
        if spec["type"] == "equity_index":
            frame = ak.stock_zh_index_daily(symbol=spec["symbol"])
            date_col, close_col = "date", "close"
        else:
            frame = ak.fund_etf_hist_em(
                symbol=spec["symbol"], period="daily",
                start_date=spec["since"],
                end_date=datetime.now(TZ).strftime("%Y%m%d"), adjust="",
            )
            date_col, close_col = "日期", "收盘"
        result_queue.put({
            "ok": True,
            "version": str(getattr(ak, "__version__", "unknown")),
            "columns": list(frame.columns) if frame is not None else [],
            "date_col": date_col, "close_col": close_col,
            "records": [] if frame is None else frame.to_dict(orient="records"),
        })
    except Exception as exc:
        result_queue.put({"ok": False, "error": type(exc).__name__ + ": " + str(exc)})


def fetch_history(asset_id: str, timeout_seconds: int = 65, fetcher=None) -> dict:
    if asset_id not in ASSETS:
        raise AssetDataError("不支持的观察标的: " + str(asset_id))
    if fetcher is None:
        ctx = multiprocessing.get_context("spawn")
        results = ctx.Queue(maxsize=1)
        process = ctx.Process(target=_worker, args=(asset_id, results), daemon=True)
        process.start()
        try:
            try:
                payload = results.get(timeout=timeout_seconds)
            except queue.Empty as exc:
                raise AssetDataError("AKShare指数/ETF请求超时或子进程异常") from exc
        finally:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            results.close()
    else:
        payload = fetcher(asset_id)
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise AssetDataError("上游行情接口失败: " + str((payload or {}).get("error", "未知错误")))
    cols = set(payload.get("columns") or [])
    dcol, vcol = payload.get("date_col"), payload.get("close_col")
    if not dcol or not vcol or not {dcol, vcol}.issubset(cols):
        raise AssetDataError("上游历史行情字段结构改变，拒绝写库")
    rows = []
    seen = set()
    earliest = _canonical_date(ASSETS[asset_id]["since"])
    for record in payload.get("records") or []:
        day = _canonical_date(record.get(dcol))
        if day < earliest:
            # Some indexes are back-calculated before the formal launch; do not
            # silently load dates before the configured scope.
            continue
        if day > datetime.now(TZ).date().isoformat():
            raise AssetDataError("返回包含未来日期，拒绝更新")
        try:
            level = float(record.get(vcol))
        except (ValueError, TypeError, OverflowError):
            raise AssetDataError("收盘价包含无法识别的值：" + day)
        if not math.isfinite(level) or level <= 0:
            raise AssetDataError("收盘价非正数或非有限值：" + day)
        if day in seen:
            raise AssetDataError("行情历史存在重复日期：" + day)
        seen.add(day)
        rows.append((day, level))
    rows.sort()
    if len(rows) < 30:
        raise AssetDataError("有效历史少于30个交易日，拒绝更新")
    if (datetime.now(TZ).date() - date.fromisoformat(rows[-1][0])).days > 45:
        raise AssetDataError("数据源最新日期超过45天前，拒绝作为近期行情激活")
    return {
        "asset_id": asset_id, "meta": ASSETS[asset_id],
        "rows": rows, "version": str(payload.get("version") or "unknown"),
        "first_date": rows[0][0], "last_date": rows[-1][0],
    }


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS asset_series (
            asset_id TEXT PRIMARY KEY,
            source_interface TEXT NOT NULL, source_name TEXT NOT NULL,
            source_version TEXT, unit TEXT NOT NULL, method TEXT NOT NULL,
            first_date TEXT NOT NULL, last_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS asset_close (
            asset_id TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
            PRIMARY KEY(asset_id, date)
        );
    """)
    return connection


def update_history(asset_id: str, *, db_path: str | Path = DEFAULT_DB, fetcher=None):
    if asset_id not in ASSETS:
        raise AssetDataError("未知标的")
    fresh = fetch_history(asset_id, fetcher=fetcher)
    with closing(connect(db_path)) as db, db:
        previous = db.execute(
            "SELECT first_date,last_date FROM asset_series WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if previous and (
            fresh["first_date"] > previous["first_date"]
            or fresh["last_date"] < previous["last_date"]
        ):
            raise AssetDataError("新数据历史比当前活跃历史短，拒绝覆盖已保存的行情")
        # Merge only from the identical explicit source and methodology.
        # No provider fallback or price-source stitching.
        spec = fresh["meta"]
        existing = db.execute(
            "SELECT source_interface,method FROM asset_series WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if existing and (existing["source_interface"], existing["method"]) != (
            spec["source_interface"], spec["method"]
        ):
            raise AssetDataError("行情来源或方法改变，拒绝静默拼接")
        db.executemany(
            "INSERT INTO asset_close(asset_id,date,close) VALUES(?,?,?) "
            "ON CONFLICT(asset_id,date) DO UPDATE SET close=excluded.close",
            ((asset_id, day, close) for day, close in fresh["rows"]),
        )
        db.execute("""
            INSERT INTO asset_series(asset_id,source_interface,source_name,source_version,
                                     unit,method,first_date,last_date,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_id) DO UPDATE SET
                source_version=excluded.source_version,
                first_date=excluded.first_date,last_date=excluded.last_date,
                fetched_at=excluded.fetched_at
        """, (
            asset_id, spec["source_interface"], spec["source_name"], fresh["version"],
            spec["unit"], spec["method"], fresh["first_date"], fresh["last_date"],
            datetime.now(timezone.utc).isoformat(),
        ))
    return {"asset": asset_id, "name": ASSETS[asset_id]["name"],
            "count": len(fresh["rows"]), "last_date": fresh["last_date"],
            "source": fresh["meta"]["source_interface"]}


def _statistics(rows: list[dict], span: str) -> dict:
    if not rows:
        return {"available": False, "reason": "无真实数据"}
    last = date.fromisoformat(rows[-1]["date"])
    periods = {"3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60, "8y": 96, "10y": 120}
    if span not in periods and span != "all":
        raise AssetDataError("不支持的区间")
    if span == "all":
        selected = rows
    else:
        months = periods[span]
        absolute = last.year * 12 + last.month - 1 - months
        year, month0 = divmod(absolute, 12)
        cutoff = date(year, month0 + 1, min(last.day, 28)).isoformat()
        before = [i for i, row in enumerate(rows) if row["date"] <= cutoff]
        if not before:
            return {"available": False, "reason": "历史记录不足以覆盖" + span}
        start_i = before[-1]
        if (date.fromisoformat(rows[start_i]["date"]) - date.fromisoformat(cutoff)).days > 14:
            return {"available": False, "reason": "区间起点存在大幅数据缺口"}
        selected = rows[start_i:]
    if len(selected) < 2:
        return {"available": False, "reason": "有效历史不足"}
    price0, price1 = selected[0]["close"], selected[-1]["close"]
    peak = selected[0]["close"]
    worst = 0.0
    for item in selected:
        peak = max(peak, item["close"])
        worst = min(worst, item["close"]/peak - 1.0)
    return {
        "available": True, "start": selected[0]["date"], "end": selected[-1]["date"],
        "samples": len(selected), "return_pct": (price1/price0 - 1.0)*100,
        "max_drawdown_pct": worst*100, "last_close": price1,
        "high": max(item["close"] for item in selected),
        "low": min(item["close"] for item in selected),
    }


def report(asset_id: str, db_path: str | Path = DEFAULT_DB) -> dict:
    if asset_id not in ASSETS:
        raise AssetDataError("未知观察标的")
    meta = ASSETS[asset_id]
    if not Path(db_path).exists():
        return {"available": False, "asset": asset_id, "meta": meta, "message": "尚未采集真实价格", "rows": [], "stats": {}}
    with closing(connect(db_path)) as db:
        source = db.execute("SELECT * FROM asset_series WHERE asset_id=?", (asset_id,)).fetchone()
        if source is None:
            return {"available": False, "asset": asset_id, "meta": meta, "message": "尚未采集真实价格", "rows": [], "stats": {}}
        rows = [dict(row) for row in db.execute(
            "SELECT date, close FROM asset_close WHERE asset_id=? ORDER BY date",
            (asset_id,)
        )]
        source = dict(source)
    return {
        "available": True, "asset": asset_id, "meta": meta, "source": source,
        "rows": rows,
        "stats": {window: _statistics(rows, window) for window in ("3m", "6m", "1y", "3y", "5y", "8y", "10y", "all")},
        "notice": "仅价格变化，不含ETF分红再投资或交易费用；不提供PE/PB。场内黄金ETF价格不能替代黄金现货。",
    }


def overview(db_path: str | Path = DEFAULT_DB) -> dict:
    return {
        "assets": [
            {"asset": code, "name": spec["name"], "type": spec["type"],
             "available": (result := report(code, db_path))["available"],
             "last_date": result.get("source", {}).get("last_date"),
             "count": len(result["rows"])}
            for code, spec in ASSETS.items()
        ],
    }
