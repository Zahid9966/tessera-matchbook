"""DuckDB warehouse: one process-wide connection, cursors per caller, plus a `meta` schema
for run history, quality results, saved metrics and agent state."""
from __future__ import annotations
import threading, duckdb
from . import config

_lock = threading.RLock()
_con: duckdb.DuckDBPyConnection | None = None

META_DDL = """
create schema if not exists raw; create schema if not exists staging; create schema if not exists marts;
create schema if not exists meta;
create sequence if not exists meta.seq;
create table if not exists meta.ingest_files(file varchar primary key, sha varchar, rows bigint, loaded_at timestamp);
create table if not exists meta.runs(run_id bigint primary key, task varchar, status varchar, started_at timestamp,
    finished_at timestamp, detail varchar, trigger varchar);
create table if not exists meta.quality_results(run_id bigint, model varchar, column_name varchar, test varchar,
    severity varchar, status varchar, failures bigint, checked_at timestamp);
create table if not exists meta.saved_metrics(name varchar primary key, description varchar, sql varchar, created_by varchar, created_at timestamp);
create table if not exists meta.approvals(id bigint primary key, tool varchar, args varchar, status varchar, created_at timestamp, decided_at timestamp, result varchar);
create table if not exists meta.threads(id bigint primary key, title varchar, created_at timestamp);
create table if not exists meta.messages(id bigint primary key, thread_id bigint, role varchar, content varchar, tool_calls varchar, created_at timestamp);
create table if not exists meta.memory(id bigint primary key, kind varchar, content varchar, embedding float[], source_thread bigint, created_at timestamp);
create table if not exists meta.memory_settings(key varchar primary key, value varchar);
create table if not exists meta.audit(id bigint primary key, ts timestamp, actor varchar, action varchar, detail varchar);
"""

def connect() -> duckdb.DuckDBPyConnection:
    global _con
    with _lock:
        if _con is None:
            config.ensure_dirs()
            _con = duckdb.connect(str(config.WAREHOUSE))
            _con.execute(META_DDL)
            for k, v in {"memory_enabled": "true", "store_sql_results": "false", "max_rows": "500"}.items():
                _con.execute("insert into meta.memory_settings values (?,?) on conflict do nothing", [k, v])
        return _con

def close() -> None:
    global _con
    with _lock:
        if _con is not None: _con.close(); _con = None

def cursor() -> duckdb.DuckDBPyConnection:
    return connect().cursor()

def nextid() -> int:
    return cursor().execute("select nextval('meta.seq')").fetchone()[0]

def query(sql: str, params: list | None = None) -> list[dict]:
    cur = cursor().execute(sql, params or [])
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def audit(actor: str, action: str, detail: str = "") -> None:
    cursor().execute("insert into meta.audit values (nextval('meta.seq'), now(), ?, ?, ?)", [actor, action, detail[:2000]])

def setting(key: str, default: str = "") -> str:
    r = cursor().execute("select value from meta.memory_settings where key=?", [key]).fetchone()
    return r[0] if r else default
