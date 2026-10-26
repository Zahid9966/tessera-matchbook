"""Postgres access: a small connection pool, forward-only migrations, and a private dev/test cluster."""
from __future__ import annotations
import glob, os, re, shutil, socket, subprocess, tempfile
from contextlib import contextmanager
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from .settings import settings

MIGRATIONS = Path(__file__).parent / "migrations"
_pool: ConnectionPool | None = None

class NotConfigured(RuntimeError): ...

def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not settings.database_url: raise NotConfigured("MATCHBOOK_DATABASE_URL is not set")
        _pool = ConnectionPool(settings.database_url, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
        _pool.wait(timeout=15)
    return _pool

def close() -> None:
    global _pool
    if _pool is not None: _pool.close(); _pool = None

@contextmanager
def tx():
    """One transaction: commits on success, rolls back on any exception."""
    with pool().connection() as conn:
        yield conn

def q(sql: str, params=()) -> list[dict]:
    with tx() as c: return c.execute(sql, params).fetchall()

def migrate() -> list[str]:
    """Apply any missing migrations, serialised across processes by an advisory lock. Idempotent."""
    applied_now = []
    with tx() as c:
        c.execute("select pg_advisory_xact_lock(727274)")
        c.execute("create table if not exists schema_migrations(version text primary key, applied_at timestamptz not null default now())")
        done = {r["version"] for r in c.execute("select version from schema_migrations").fetchall()}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.stem not in done:
                c.execute(f.read_text()); c.execute("insert into schema_migrations(version) values (%s)", (f.stem,)); applied_now.append(f.stem)
    return applied_now

def reset_for_tests() -> None:
    """Drop everything and migrate again. Refuses to run unless the database name says it is disposable."""
    with tx() as c:
        name = c.execute("select current_database() as d").fetchone()["d"]
        if not re.search(r"(test|dev|tmp)", name): raise RuntimeError(f"refusing to reset non-disposable database '{name}'")
        c.execute("drop schema public cascade; create schema public")
    migrate()

# ---- private throwaway cluster (dev + tests). Never touches any Postgres you already run. -------------------------------
def _bin(name: str) -> str:
    found = shutil.which(name) or next(iter(sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"))[::-1]), None)
    if not found: raise FileNotFoundError(f"{name} not found; install postgresql server binaries")
    return found

def _free_port() -> int:
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

def start_dev_cluster(root: str | None = None, dbname: str = "matchbook_dev") -> tuple[str, str]:
    """initdb + start a cluster on a free localhost port with trust auth. Returns (database_url, data_dir)."""
    root = root or tempfile.mkdtemp(prefix="mbpg_"); data = os.path.join(root, "data"); sock = os.path.join(root, "sock"); os.makedirs(sock, exist_ok=True)
    if not os.path.exists(os.path.join(data, "PG_VERSION")):
        subprocess.run([_bin("initdb"), "-D", data, "-U", "matchbook", "--auth=trust", "-E", "UTF8"], check=True, capture_output=True)
    port = _free_port()
    subprocess.run([_bin("pg_ctl"), "-D", data, "-o", f"-p {port} -k {sock} -c listen_addresses=127.0.0.1", "-l", os.path.join(root, "pg.log"), "-w", "start"], check=True, capture_output=True)
    with psycopg.connect(f"postgresql://matchbook@127.0.0.1:{port}/postgres", autocommit=True) as c:
        if not c.execute("select 1 from pg_database where datname=%s", (dbname,)).fetchone(): c.execute(f'create database "{dbname}"')
    return f"postgresql://matchbook@127.0.0.1:{port}/{dbname}", data

def stop_dev_cluster(data_dir: str, remove: bool = False) -> None:
    subprocess.run([_bin("pg_ctl"), "-D", data_dir, "-m", "fast", "-w", "stop"], capture_output=True)
    if remove: shutil.rmtree(os.path.dirname(data_dir), ignore_errors=True)

def ensure_dev_cluster(root: str, dbname: str = "matchbook_dev") -> str:
    """Reuse a persistent private cluster under `root` if it is running, otherwise start it. Returns the database URL."""
    data = os.path.join(root, "data"); pidfile = os.path.join(data, "postmaster.pid")
    if os.path.exists(pidfile):
        lines = open(pidfile).read().splitlines()
        try:
            port = int(lines[3])
            with psycopg.connect(f"postgresql://matchbook@127.0.0.1:{port}/postgres", connect_timeout=2): return f"postgresql://matchbook@127.0.0.1:{port}/{dbname}"
        except Exception: pass                                   # stale pid file: fall through and start it
    os.makedirs(root, exist_ok=True)
    return start_dev_cluster(root, dbname)[0]

if __name__ == "__main__":
    import sys
    print(ensure_dev_cluster(sys.argv[1]))
