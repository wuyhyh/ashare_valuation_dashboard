from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading


SCHEMA_VERSION = 3
_migration_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS series (
            series_id TEXT PRIMARY KEY,
            index_code TEXT NOT NULL,
            metric TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_interface TEXT NOT NULL,
            methodology TEXT NOT NULL,
            frequency TEXT NOT NULL,
            quality_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_series_index_metric ON series(index_code, metric);

        CREATE TABLE IF NOT EXISTS observations (
            series_id TEXT NOT NULL,
            index_code TEXT NOT NULL,
            metric TEXT NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(series_id, date),
            FOREIGN KEY(series_id) REFERENCES series(series_id)
        );
        CREATE INDEX IF NOT EXISTS idx_observations_lookup
            ON observations(index_code, metric, series_id, date);

        CREATE TABLE IF NOT EXISTS active_series (
            index_code TEXT NOT NULL,
            metric TEXT NOT NULL,
            series_id TEXT NOT NULL,
            PRIMARY KEY(index_code, metric),
            FOREIGN KEY(series_id) REFERENCES series(series_id)
        );

        CREATE TABLE IF NOT EXISTS update_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            index_code TEXT NOT NULL,
            metric TEXT NOT NULL,
            series_id TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            valid_count INTEGER NOT NULL DEFAULT 0,
            first_date TEXT,
            last_date TEXT,
            source_version TEXT,
            columns_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_update_runs_lookup
            ON update_runs(index_code, metric, id DESC);
    """)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _legacy_series_id(index_code: str, metric: str, source: str) -> str:
    source_hash = hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]
    return f"legacy_{index_code}_{metric}_{source_hash}"


def _legacy_methodology(metric: str, source: str) -> tuple[str, str]:
    if "AKShare" in source or "乐咕" in source:
        interface = "ak.stock_index_pe_lg" if metric == "pe_ttm" else "ak.stock_index_pb_lg"
        field = "滚动市盈率" if metric == "pe_ttm" else "市净率"
        methodology = f"旧库迁移：AKShare 字段“{field}”；未与等权、静态或中位数口径混用"
        return interface, methodology
    return "legacy.local", "旧库迁移：来源口径以原始 source 字段为准，使用前需人工复核"


def _migrate_legacy(db_path: Path, connection: sqlite3.Connection) -> Path:
    connection.execute("PRAGMA wal_checkpoint(FULL)")
    connection.commit()
    backup = db_path.with_name(f"{db_path.name}.backup-v0.2-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(db_path, backup)
    connection.execute("ALTER TABLE observations RENAME TO observations_legacy_v02")
    _create_schema(connection)
    groups = connection.execute("""
        SELECT index_code, metric, source, MIN(fetched_at), MAX(fetched_at)
        FROM observations_legacy_v02 GROUP BY index_code, metric, source
    """).fetchall()
    active_candidates: dict[tuple[str, str], tuple[str, str]] = {}
    for index_code, metric, source, first_seen, last_seen in groups:
        series_id = _legacy_series_id(index_code, metric, source)
        interface, methodology = _legacy_methodology(metric, source)
        connection.execute("""
            INSERT INTO series(series_id,index_code,metric,source_name,source_interface,
                               methodology,frequency,quality_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (series_id, index_code, metric, source, interface, methodology, "unknown", "{}",
              first_seen or utc_now(), last_seen or utc_now()))
        connection.execute("""
            INSERT INTO observations(series_id,index_code,metric,date,value,source,fetched_at)
            SELECT ?,index_code,metric,date,value,source,fetched_at
            FROM observations_legacy_v02
            WHERE index_code=? AND metric=? AND source=?
        """, (series_id, index_code, metric, source))
        key = (index_code, metric)
        if key not in active_candidates or (last_seen or "") > active_candidates[key][0]:
            active_candidates[key] = (last_seen or "", series_id)
    for (index_code, metric), (_, series_id) in active_candidates.items():
        connection.execute("INSERT INTO active_series VALUES(?,?,?)", (index_code, metric, series_id))
    connection.commit()
    return backup


def ensure_schema(db_path: str | Path) -> Path | None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _migration_lock:
        connection = sqlite3.connect(path, timeout=20)
        try:
            tables = _tables(connection)
            if "observations" in tables:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(observations)")}
                if "series_id" not in columns:
                    return _migrate_legacy(path, connection)
            _create_schema(connection)
            connection.commit()
            return None
        finally:
            connection.close()


def connect(db_path: str | Path) -> sqlite3.Connection:
    ensure_schema(db_path)
    connection = sqlite3.connect(db_path, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def record_update_run(db_path: str | Path, record: dict) -> None:
    with closing(connect(db_path)) as connection, connection:
        connection.execute("""
            INSERT INTO update_runs(index_code,metric,series_id,started_at,finished_at,status,
                fetched_count,valid_count,first_date,last_date,source_version,columns_json,
                warnings_json,error)
            VALUES(:index_code,:metric,:series_id,:started_at,:finished_at,:status,
                :fetched_count,:valid_count,:first_date,:last_date,:source_version,:columns_json,
                :warnings_json,:error)
        """, {
            "series_id": None, "fetched_count": 0, "valid_count": 0,
            "first_date": None, "last_date": None, "source_version": None,
            "columns_json": "[]", "warnings_json": "[]", "error": None,
            **record,
        })


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
