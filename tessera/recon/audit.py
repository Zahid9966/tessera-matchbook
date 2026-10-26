"""Append-only, hash-chained audit trail in Postgres.

Each row stores hash = sha256(prev_hash || canonical row content). Rows cannot be updated or deleted (a trigger refuses),
and verify() recomputes the chain, so editing history with direct database access is detectable, not only discouraged."""
from __future__ import annotations
import hashlib, json
from . import pg

GENESIS = "0" * 64

def _digest(prev: str, ts: str, actor: str, actor_id, action: str, detail: dict, ip) -> str:
    body = json.dumps([ts, actor, actor_id, action, detail, ip], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + body).encode()).hexdigest()

def log(actor: str, action: str, detail: dict | None = None, actor_id: int | None = None, ip: str | None = None, conn=None) -> None:
    """Write one entry. Pass conn to make it part of the caller's transaction (so an action and its audit row commit together)."""
    def _do(c):
        c.execute("select pg_advisory_xact_lock(727275)")                      # serialise appends so the chain has no forks
        prev = c.execute("select hash from audit_log order by id desc limit 1").fetchone()
        prev_hash = prev["hash"] if prev else GENESIS
        ts = c.execute("select to_char(now() at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') as t").fetchone()["t"]
        d = detail or {}
        c.execute("insert into audit_log(ts, actor, actor_id, action, detail, ip, prev_hash, hash) values (%s::timestamp at time zone 'utc',%s,%s,%s,%s::jsonb,%s,%s,%s)",
                  (ts, actor, actor_id, action, json.dumps(d, default=str), ip, prev_hash, _digest(prev_hash, ts, actor, actor_id, action, json.loads(json.dumps(d, default=str)), ip)))
    if conn is not None: _do(conn)
    else:
        with pg.tx() as c: _do(c)

def verify() -> dict:
    """Recompute the whole chain. Returns {ok, rows, first_bad_id}."""
    prev, n = GENESIS, 0
    with pg.tx() as c:
        for r in c.execute("select id, to_char(ts at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') as t, actor, actor_id, action, detail, ip, prev_hash, hash from audit_log order by id").fetchall():
            n += 1
            if r["prev_hash"] != prev or r["hash"] != _digest(prev, r["t"], r["actor"], r["actor_id"], r["action"], r["detail"], r["ip"]):
                return {"ok": False, "rows": n, "first_bad_id": r["id"]}
            prev = r["hash"]
    return {"ok": True, "rows": n, "first_bad_id": None}

def recent(limit: int = 100) -> list[dict]:
    return pg.q("select id, to_char(ts, 'YYYY-MM-DD HH24:MI:SS') as ts, actor, action, detail from audit_log order by id desc limit %s", (min(limit, 500),))
