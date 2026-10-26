"""Analyst agent turn runner with a durable event stream.

A turn yields events (token, tool_call, tool_result, approval_required, answer, error). Every message is
persisted to meta.messages, so a turn can pause on an approval card and resume later - the same shape as
the 'journal' idea: the stored history is the source of truth, the HTTP stream is just a view of it."""
from __future__ import annotations
import json
from typing import Iterator
from .. import warehouse
from . import gateway, memory, tools

MAX_STEPS = 6

def _system() -> str:
    from ..pipeline import models
    ms = models.load()
    from .. import warehouse as wh
    pii = models.pii_columns(); lines = []
    for m in ms.values():
        if m.layer != "marts": continue
        cols = [r["column_name"] for r in wh.query("select column_name from information_schema.columns where table_schema='marts' and table_name=? order by ordinal_position", [m.name]) if r["column_name"] not in pii.get(m.name, set())]
        lines.append(f"- marts.{m.name}({', '.join(cols)}): {m.description}")
    cat = "\n".join(lines)
    return f"""You are Tessera, a careful data analyst for an e-commerce warehouse (DuckDB). Today is 2026-10-19.
Rules:
- Never invent numbers. Get every figure by calling run_sql, then explain the result briefly in plain language.
- Prefer marts.* tables. Always schema-qualify tables. One SELECT per run_sql call. If a query errors, fix it and retry.
- If unsure about columns, call describe_table first. Customer names/emails are PII and are not available.
- Revenue means net_revenue on completed orders unless the user says otherwise. Check search_memory for user-defined terms when a term is ambiguous.
- Changing anything (saving metrics, running pipelines, remembering notes) is a write tool and needs the user's approval.
- Stay on data and analytics topics. Be concise: lead with the answer, then 1-3 supporting bullets.
Tables and their exact columns (use only these names):
{cat}"""

def _save(thread: int, role: str, content: str = "", tool_calls=None) -> None:
    warehouse.cursor().execute("insert into meta.messages values (nextval('meta.seq'),?,?,?,?,now())",
                               [thread, role, content, json.dumps(tool_calls) if tool_calls is not None else None])

def _history(thread: int) -> list[dict]:
    msgs = [{"role": "system", "content": _system()}]
    for r in warehouse.query("select role, content, tool_calls from meta.messages where thread_id=? order by id", [thread]):
        tc = json.loads(r["tool_calls"]) if r["tool_calls"] else None
        if r["role"] == "assistant": msgs.append({"role": "assistant", "content": r["content"], **({"tool_calls": [{"function": f} for f in tc]} if tc else {})})
        elif r["role"] == "tool": msgs.append({"role": "tool", "content": r["content"], "tool_name": (tc or {}).get("name", "")})
        else: msgs.append({"role": r["role"], "content": r["content"]})
    return msgs

def new_thread(title: str = "New analysis") -> int:
    tid = warehouse.nextid(); warehouse.cursor().execute("insert into meta.threads values (?,?,now())", [tid, title]); return tid

def _compact(result: dict) -> str:
    """Trim tool output for the model's context (the UI still receives the full rows)."""
    r = dict(result)
    if isinstance(r.get("rows"), list) and len(r["rows"]) > 25:
        r["rows"] = r["rows"][:25]; r["note"] = f"showing 25 of {result['row_count']} rows"
    return json.dumps(r, default=str)[:6000]

import re
_NUM = re.compile(r"\d[\d,]*\.?\d*")

def ungrounded_numbers(answer: str, evidence: str) -> list[str]:
    """Numbers quoted in the answer that appear nowhere in this turn's tool output (small models misread digits)."""
    ev = {n.replace(",", "").rstrip(".") for n in _NUM.findall(evidence)}
    ev_f = set()
    for n in ev:
        try: ev_f.add(round(float(n), 2)); ev_f.add(round(float(n), 0))
        except ValueError: pass
    bad = []
    for n in _NUM.findall(answer):
        s = n.replace(",", "").rstrip(".")
        try: v = float(s)
        except ValueError: continue
        if v < 10 and "." not in s: continue          # ranks, list numbering
        if s not in ev and round(v, 2) not in ev_f and round(v, 0) not in ev_f: bad.append(n)
    return bad

def _evidence(thread: int) -> str:
    rows = warehouse.query("select content from meta.messages where thread_id=? and role='tool' and id > coalesce((select max(id) from meta.messages where thread_id=? and role='user'),0)", [thread, thread])
    return " ".join(r["content"] for r in rows)

def _loop(thread: int, question: str | None) -> Iterator[dict]:
    steps = 0
    while steps < MAX_STEPS:
        steps += 1; final = None
        try:
            for ev in gateway.chat_stream("analystChat", _history(thread), tools.specs()):
                if ev["type"] == "token": yield {"type": "token", "text": ev["text"]}
                else: final = ev
        except gateway.GatewayError as e:
            yield {"type": "error", "message": str(e)}; return
        calls = final["tool_calls"]
        _save(thread, "assistant", final["content"], calls or None)
        if not calls:
            yield {"type": "answer", "text": final["content"]}
            bad = ungrounded_numbers(final["content"], _evidence(thread))
            if bad: yield {"type": "warning", "message": f"Some figures in this answer ({', '.join(bad[:4])}) do not appear in the query results. Trust the table, not the prose."}
            if question and memory.enabled():
                for m in memory.extract_from_turn(thread, question, final["content"]):
                    if m.get("stored"): yield {"type": "memory_saved", "content": m["content"]}
            return
        for c in calls:
            name, args = c["name"], c.get("arguments") or {}
            t = tools.TOOLS.get(name)
            yield {"type": "tool_call", "name": name, "args": args, "access": t.access if t else "unknown"}
            if t and t.access == "write":
                aid = warehouse.nextid()
                warehouse.cursor().execute("insert into meta.approvals values (?,?,?,'pending',now(),null,null)", [aid, name, json.dumps(args)])
                _save(thread, "tool", json.dumps({"status": "awaiting_approval", "approval_id": aid}), {"name": name, "approval": aid})
                yield {"type": "approval_required", "approval_id": aid, "tool": name, "args": args}
                return
            res = tools.execute(name, args)
            _save(thread, "tool", _compact(res), {"name": name})
            yield {"type": "tool_result", "name": name, "result": res}

def run_turn(thread: int, question: str) -> Iterator[dict]:
    _save(thread, "user", question)
    warehouse.cursor().execute("update meta.threads set title=? where id=? and title='New analysis'", [question[:60], thread])
    yield from _loop(thread, question)

def decide(approval_id: int, approve: bool) -> Iterator[dict]:
    rows = warehouse.query("select * from meta.approvals where id=? and status='pending'", [approval_id])
    if not rows: yield {"type": "error", "message": "approval not found or already decided"}; return
    a = rows[0]; thread_rows = warehouse.query("select thread_id from meta.messages where tool_calls like ? and role='tool' limit 1", [f'%"approval": {approval_id}%'])
    thread = thread_rows[0]["thread_id"]
    if approve:
        res = tools.execute(a["tool"], json.loads(a["args"])); status = "approved"
    else:
        res, status = {"declined": True, "note": "the user declined this action"}, "declined"
    warehouse.cursor().execute("update meta.approvals set status=?, decided_at=now(), result=? where id=?", [status, json.dumps(res, default=str)[:2000], approval_id])
    warehouse.audit("user", f"approval:{status}", f"{a['tool']} {a['args']}"[:500])
    # Replace the awaiting placeholder with the real outcome, then let the model continue.
    warehouse.cursor().execute("update meta.messages set content=? where role='tool' and tool_calls like ?", [json.dumps(res, default=str), f'%"approval": {approval_id}%'])
    yield {"type": "tool_result", "name": a["tool"], "result": res}
    yield from _loop(thread, None)
