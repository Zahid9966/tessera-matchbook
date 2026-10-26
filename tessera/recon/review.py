"""Human review with maker/checker (four-eyes).

A decision is a PROPOSAL by a maker. It has no effect until a DIFFERENT user with the approver role approves it.
Per item, approved decisions form a stack (confirm / reject / link push, an approved undo pops). State lives in Postgres;
the only side effect on analytics data is the manual link, which is rebuilt from approved decisions, so Postgres stays the truth.
Items are keyed by stable identifiers, so decisions survive a re-import."""
from __future__ import annotations
import datetime as dt
import json
import psycopg.errors
from .. import warehouse
from . import audit, auth, match, pg
from .settings import settings

KINDS = {"ledger_without_payment", "gateway_without_order", "batch_without_bank_credit", "credit_without_batch", "settlement_amount_difference", "flag"}
LABEL = {"confirm": "confirmed", "reject": "rejected", "link": "linked"}

class ReviewError(ValueError):
    def __init__(self, message: str, status: int = 400): super().__init__(message); self.status = status

def loaded() -> bool:
    need = {"exceptions", "flag_ranked", "match", "ledger"}
    have = {r["table_name"] for r in warehouse.query("select table_name from information_schema.tables where table_schema='recon'")}
    return need <= have

FLAG_BASE = """select *, md5(cast(posted_on as varchar) || '|' || payee || '|' || debit_paise || '|' || row_number() over (partition by posted_on, payee, debit_paise order by row_id)) as item_key from recon.flag_ranked"""

# ---- state -------------------------------------------------------------------------------------------------------------
def _stacks(kind: str | None = None) -> dict[tuple, list[dict]]:
    rows = pg.q("select * from decisions where status='approved'" + (" and kind=%s" if kind else "") + " order by decided_at, id", (kind,) if kind else ())
    st: dict[tuple, list[dict]] = {}
    for e in rows:
        s = st.setdefault((e["kind"], e["item_key"]), [])
        if e["action"] == "undo":
            if s: s.pop()
        else: s.append(e)
    return st

def _proposals(kind: str | None = None) -> dict[tuple, dict]:
    rows = pg.q("select d.id, d.kind, d.item_key, d.action, d.note, d.payload, d.proposed_by, u.username as proposed_by_name, to_char(d.proposed_at, 'YYYY-MM-DD HH24:MI') as proposed_at "
                "from decisions d join users u on u.id = d.proposed_by where d.status='proposed'" + (" and d.kind=%s" if kind else ""), (kind,) if kind else ())
    return {(r["kind"], r["item_key"]): r for r in rows}

def statuses(kind: str) -> dict[str, dict]:
    out = {}
    stacks, props = _stacks(kind), _proposals(kind)
    for (k, key), s in stacks.items():
        if s: out[key] = {"status": LABEL[s[-1]["action"]], "note": s[-1]["note"] or "", "can_undo": True}
    for (k, key), p in props.items():
        out.setdefault(key, {"status": "pending", "note": "", "can_undo": False})["proposal"] = {"id": p["id"], "action": p["action"], "by": p["proposed_by_name"], "by_id": p["proposed_by"], "at": p["proposed_at"], "note": p["note"]}
    return out

def _effective_status(kind: str, key: str) -> str:
    s = _stacks(kind).get((kind, key), []); return LABEL[s[-1]["action"]] if s else "pending"

def effective_links() -> list[dict]:
    return [json.loads(s[-1]["payload"]) if isinstance(s[-1]["payload"], str) else s[-1]["payload"]
            for (k, _), s in _stacks("ledger_without_payment").items() if s and s[-1]["action"] == "link"]

def sync_links() -> int:
    """Rebuild the DuckDB manual_link table from approved decisions (Postgres is the source of truth), then re-run matching."""
    links = effective_links(); cur = warehouse.cursor()
    cur.execute("create table if not exists recon.manual_link(order_id varchar, payment_id varchar, linked_at timestamp default now())")
    cur.execute("delete from recon.manual_link")
    for l in links: cur.execute("insert into recon.manual_link(order_id, payment_id) values (?,?)", [l["order_id"], l["payment_id"]])
    have = {r["table_name"] for r in warehouse.query("select table_name from information_schema.tables where table_schema='recon'")}
    if {"ledger", "gateway_line", "bank"} <= have: match.run()       # also covers the first import, when no match tables exist yet
    return len(links)

# ---- propose / approve / decline / withdraw ---------------------------------------------------------------------------------
def _exists(kind: str, key: str) -> bool:
    if kind == "flag": return bool(warehouse.query(f"select 1 from ({FLAG_BASE}) where item_key = ?", [key]))
    return bool(warehouse.query("select 1 from recon.exceptions where kind=? and item_key=?", [kind, key]))

def propose(user: auth.User, kind: str, key: str, action: str, note: str = "", payload: dict | None = None, ip: str | None = None) -> dict:
    if not user.can("propose"): raise ReviewError("your role cannot propose decisions", 403)
    if kind not in KINDS: raise ReviewError(f"unknown item kind '{kind}'")
    if action not in ("confirm", "reject", "link", "undo"): raise ReviewError("action must be confirm, reject, link or undo")
    cur = _effective_status(kind, key)
    if action == "undo":
        if cur == "pending": raise ReviewError("nothing to undo", 409)
    else:
        if not _exists(kind, key): raise ReviewError("no such item", 404)
        if LABEL[action] == cur: raise ReviewError(f"already {cur}", 409)
    auto = not settings.require_checker
    try:
        with pg.tx() as c:
            did = c.execute("insert into decisions(kind, item_key, action, note, payload, proposed_by, status, decided_by, decided_at, auto_approved) values (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) returning id",
                            (kind, key, action, (note or "")[:500], json.dumps(payload) if payload else None, user.id, "approved" if auto else "proposed",
                             user.id if auto else None, dt.datetime.now(dt.timezone.utc) if auto else None, auto)).fetchone()["id"]
            audit.log(user.username, "decision.auto_approved" if auto else "decision.proposed", {"id": did, "kind": kind, "key": key, "action": action, "note": note[:200]}, user.id, ip, conn=c)
    except psycopg.errors.UniqueViolation:
        p = _proposals(kind).get((kind, key)); raise ReviewError(f"already has an open proposal by {p['proposed_by_name'] if p else 'someone'}; approve, decline or withdraw it first", 409)
    if auto and action in ("link", "undo"): sync_links()
    return {"id": did, "status": "approved" if auto else "proposed", "auto_approved": auto}

def propose_link(user: auth.User, order_id: str, payment_id: str, note: str = "", ip: str | None = None) -> dict:
    if not warehouse.query("select 1 from recon.exceptions where kind='ledger_without_payment' and item_key=?", [order_id]): raise ReviewError("order is not an open exception", 409)
    if not warehouse.query("select 1 from recon.exceptions where kind='gateway_without_order' and item_key=?", [payment_id]): raise ReviewError("payment is not an open exception", 409)
    if pg.q("select 1 from decisions where status='proposed' and action='link' and payload->>'payment_id' = %s", (payment_id,)): raise ReviewError("that payment is already in another open link proposal", 409)
    if payment_id in {l["payment_id"] for l in effective_links()}: raise ReviewError("that payment is already linked", 409)
    return propose(user, "ledger_without_payment", order_id, "link", note or f"link to {payment_id}", {"order_id": order_id, "payment_id": payment_id}, ip)

def approve(user: auth.User, decision_id: int, note: str = "", ip: str | None = None) -> dict:
    if not user.can("approve"): raise ReviewError("your role cannot approve decisions", 403)
    with pg.tx() as c:
        d = c.execute("select * from decisions where id=%s for update", (decision_id,)).fetchone()
        if not d: raise ReviewError("no such proposal", 404)
        if d["status"] != "proposed": raise ReviewError(f"proposal is already {d['status']}", 409)
        if settings.require_checker and d["proposed_by"] == user.id: raise ReviewError("four-eyes: you cannot approve your own proposal", 403)
        if d["action"] == "link":
            p = d["payload"]
            if not _exists("ledger_without_payment", p["order_id"]) or not _exists("gateway_without_order", p["payment_id"]): raise ReviewError("the order or payment is no longer an open exception (data changed); decline this proposal", 409)
        c.execute("update decisions set status='approved', decided_by=%s, decided_at=now(), decision_note=%s where id=%s", (user.id, (note or "")[:500], decision_id))
        audit.log(user.username, "decision.approved", {"id": decision_id, "kind": d["kind"], "key": d["item_key"], "action": d["action"], "proposed_by": d["proposed_by"]}, user.id, ip, conn=c)
    if d["action"] in ("link", "undo"): sync_links()          # after commit: Postgres already holds the truth, DuckDB is rebuilt from it
    return {"id": decision_id, "status": "approved"}

def decline(user: auth.User, decision_id: int, note: str = "", ip: str | None = None) -> dict:
    if not user.can("approve"): raise ReviewError("your role cannot decline proposals", 403)
    with pg.tx() as c:
        d = c.execute("select * from decisions where id=%s for update", (decision_id,)).fetchone()
        if not d: raise ReviewError("no such proposal", 404)
        if d["status"] != "proposed": raise ReviewError(f"proposal is already {d['status']}", 409)
        if settings.require_checker and d["proposed_by"] == user.id: raise ReviewError("you proposed this; withdraw it instead", 403)
        c.execute("update decisions set status='declined', decided_by=%s, decided_at=now(), decision_note=%s where id=%s", (user.id, (note or "")[:500], decision_id))
        audit.log(user.username, "decision.declined", {"id": decision_id, "kind": d["kind"], "key": d["item_key"], "note": (note or "")[:200]}, user.id, ip, conn=c)
    return {"id": decision_id, "status": "declined"}

def withdraw(user: auth.User, decision_id: int, ip: str | None = None) -> dict:
    with pg.tx() as c:
        d = c.execute("select * from decisions where id=%s for update", (decision_id,)).fetchone()
        if not d: raise ReviewError("no such proposal", 404)
        if d["proposed_by"] != user.id: raise ReviewError("only the person who proposed it can withdraw it", 403)
        if d["status"] != "proposed": raise ReviewError(f"proposal is already {d['status']}", 409)
        c.execute("update decisions set status='withdrawn', decided_by=%s, decided_at=now() where id=%s", (user.id, decision_id))
        audit.log(user.username, "decision.withdrawn", {"id": decision_id, "kind": d["kind"], "key": d["item_key"]}, user.id, ip, conn=c)
    return {"id": decision_id, "status": "withdrawn"}

def open_proposals(limit: int = 100) -> list[dict]:
    rows = pg.q("select d.id, d.kind, d.item_key, d.action, d.note, d.payload, d.proposed_by, u.username as proposed_by_name, to_char(d.proposed_at, 'YYYY-MM-DD HH24:MI') as proposed_at "
                "from decisions d join users u on u.id = d.proposed_by where d.status='proposed' order by d.id limit %s", (min(limit, 500),))
    for r in rows: r["context"] = _context(r["kind"], r["item_key"])
    return rows

def _context(kind: str, key: str) -> dict:
    if not loaded(): return {}
    if kind == "flag":
        r = warehouse.query(f"select cast(posted_on as varchar) as on_date, payee, round(debit_paise/100.0,2) as amount, rules from ({FLAG_BASE}) where item_key = ?", [key])
        return r[0] if r else {}
    r = warehouse.query("select cast(on_date as varchar) as on_date, round(amount_paise/100.0,2) as amount, detail from recon.exceptions where kind=? and item_key=?", [kind, key])
    return r[0] if r else {}

# ---- listing ---------------------------------------------------------------------------------------------------------------
def candidates(order_id: str, limit: int = 8) -> list[dict]:
    if not loaded(): return []
    return warehouse.query("""
        select g.payment_id, round(g.gross_paise/100.0, 2) as amount, cast(g.captured_on as varchar) as captured_on, g.settlement_id,
               (g.gross_paise - l.amount_paise) as diff_paise, date_diff('day', l.order_date, g.captured_on) as days_after_order
        from recon.ledger l, recon.gateway_line g
        where l.order_id = ? and g.payment_id in (select item_key from recon.exceptions where kind='gateway_without_order')
          and abs(g.gross_paise - l.amount_paise) <= greatest(100, l.amount_paise * 0.02) and abs(date_diff('day', l.order_date, g.captured_on)) <= 10
        order by abs(g.gross_paise - l.amount_paise), abs(date_diff('day', l.order_date, g.captured_on)) limit ?""", [order_id, limit])

def exceptions(kind: str, state: str = "pending", limit: int = 50, offset: int = 0) -> dict:
    if kind not in KINDS - {"flag"}: raise ReviewError(f"unknown exception kind '{kind}'", 404)
    if not loaded(): return {"kind": kind, "state": state, "total": 0, "items": []}
    st = statuses(kind); effective = {k: v for k, v in st.items() if v["status"] != "pending"}
    if state == "linked":      # a linked order is no longer an exception, so list it from the approved links
        keys = [k for k, v in effective.items() if v["status"] == "linked"]
        if kind != "ledger_without_payment" or not keys: return {"kind": kind, "state": state, "total": 0, "items": []}
        rows = warehouse.query(f"""select l.order_id as item_key, round(l.amount_paise/100.0, 2) as amount, cast(l.order_date as varchar) as on_date,
                                   'Linked to payment ' || m.payment_id as detail from recon.manual_link m join recon.ledger l on l.order_id = m.order_id
                                   where m.order_id in ({",".join("?" * len(keys))}) order by l.order_id limit ? offset ?""", keys + [limit, offset])
        for r in rows: r.update(effective[r["item_key"]])
        return {"kind": kind, "state": state, "total": len(keys), "items": rows}
    where, params = "kind = ?", [kind]
    decided = sorted(effective)
    if state == "pending" and decided: where += f" and item_key not in ({','.join('?' * len(decided))})"; params += decided
    elif state in ("confirmed", "rejected"):
        keys = [k for k, v in effective.items() if v["status"] == state] or ["\0"]; where += f" and item_key in ({','.join('?' * len(keys))})"; params += keys
    total = warehouse.query(f"select count(*) as n from recon.exceptions where {where}", params)[0]["n"]
    rows = warehouse.query(f"""select item_key, round(amount_paise/100.0, 2) as amount, cast(on_date as varchar) as on_date, detail from recon.exceptions
                               where {where} order by abs(amount_paise) desc, item_key limit ? offset ?""", params + [limit, offset])
    for r in rows: r.update(st.get(r["item_key"], {"status": "pending", "note": "", "can_undo": False}))
    return {"kind": kind, "state": state, "total": total, "items": rows}

def flags(state: str = "pending", limit: int = 50, offset: int = 0) -> dict:
    if not loaded(): return {"state": state, "total": 0, "items": []}
    st = statuses("flag"); effective = {k: v for k, v in st.items() if v["status"] != "pending"}; decided = sorted(effective); where, params = "true", []
    if state == "pending" and decided: where = f"item_key not in ({','.join('?' * len(decided))})"; params = decided
    elif state in ("confirmed", "rejected"):
        keys = [k for k, v in effective.items() if v["status"] == state] or ["\0"]; where = f"item_key in ({','.join('?' * len(keys))})"; params = keys
    total = warehouse.query(f"select count(*) as n from ({FLAG_BASE}) where {where}", params)[0]["n"]
    rows = warehouse.query(f"select row_id, item_key, cast(posted_on as varchar) as posted_on, payee, round(debit_paise/100.0, 2) as amount, rules, score, reasons from ({FLAG_BASE}) where {where} order by score desc, row_id limit ? offset ?", params + [limit, offset])
    for r in rows: r.update(st.get(r["item_key"], {"status": "pending", "note": "", "can_undo": False}))
    return {"state": state, "total": total, "items": rows}

def summary() -> dict:
    if not loaded(): return {"exceptions": [], "flags": {"n": 0, "reviewed": 0, "pending": 0}, "matches": [], "counts": None, "l1_match_rate": None, "proposals_open": 0}
    out = []
    for e in warehouse.query("select kind, count(*) as n from recon.exceptions group by 1 order by 1"):
        st = statuses(e["kind"]); keys = {r["item_key"] for r in warehouse.query("select item_key from recon.exceptions where kind=?", [e["kind"]])}
        decided = sum(1 for k, v in st.items() if k in keys and v["status"] != "pending")
        out.append({**e, "reviewed": decided, "pending": e["n"] - decided, "awaiting": sum(1 for k, v in st.items() if k in keys and v.get("proposal"))})
    fl = warehouse.query("select count(*) as n from recon.flag_ranked")[0]["n"]; fst = {k: v for k, v in statuses("flag").items() if v["status"] != "pending"}
    m = warehouse.query("select layer, pass, status, count(*) as n from recon.match group by all order by 1, 2")
    counts = warehouse.query("select (select count(*) from recon.ledger) as orders, (select count(*) from recon.gateway_line) as payments, (select count(*) from recon.bank) as bank_rows")[0]
    l1 = sum(x["n"] for x in m if x["layer"] == "L1")
    return {"exceptions": out, "flags": {"n": fl, "reviewed": len(fst), "pending": max(fl - len(fst), 0)}, "matches": m, "counts": counts,
            "l1_match_rate": round(l1 / counts["orders"], 4) if counts["orders"] else None, "proposals_open": pg.q("select count(*) as n from decisions where status='proposed'")[0]["n"]}
