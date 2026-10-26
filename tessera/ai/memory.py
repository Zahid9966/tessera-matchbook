"""Analyst memory: durable notes (definitions, preferences, findings) with embeddings.
Consent-gated via meta.memory_settings.memory_enabled; users can list and hard-delete everything."""
from __future__ import annotations
import json
from .. import warehouse
from . import gateway

def enabled() -> bool:
    return warehouse.setting("memory_enabled", "true") == "true"

def remember(kind: str, content: str, thread: int | None = None) -> dict:
    if not enabled(): return {"stored": False, "reason": "memory disabled by user"}
    content = content.strip()[:600]
    dup = warehouse.query("select id from meta.memory where content = ?", [content])
    if dup: return {"stored": False, "reason": "duplicate", "id": dup[0]["id"]}
    try: emb = gateway.embed([content])[0]
    except gateway.GatewayError: emb = None
    mid = warehouse.nextid()
    warehouse.cursor().execute("insert into meta.memory values (?,?,?,?,?,now())", [mid, kind, content, emb, thread])
    return {"stored": True, "id": mid}

def search(q: str, k: int = 5) -> list[dict]:
    if not enabled(): return []
    rows = warehouse.query("select id, kind, content, created_at from meta.memory where embedding is not null limit 1")
    if rows:
        try:
            qe = gateway.embed([q])[0]
            return warehouse.query(f"""select id, kind, content, round(list_cosine_similarity(embedding, ?::float[{len(qe)}]), 3) as score
                from meta.memory where embedding is not null order by score desc limit ?""", [qe, k])
        except gateway.GatewayError: pass
    return warehouse.query("select id, kind, content, null as score from meta.memory where content ilike ? order by id desc limit ?", [f"%{q}%", k])

def list_all() -> list[dict]:
    return warehouse.query("select id, kind, content, created_at from meta.memory order by id desc")

def delete(mid: int) -> None:
    warehouse.cursor().execute("delete from meta.memory where id=?", [mid]); warehouse.audit("user", "memory_delete", str(mid))

def extract_from_turn(thread: int, question: str, answer: str) -> list[dict]:
    """After a turn, ask the memoryExtract capability for durable facts worth keeping."""
    if not enabled(): return []
    prompt = ("Extract at most 2 durable analytics facts or user preferences from this exchange (metric definitions, "
              "business rules, preferred breakdowns). Kinds allowed: definition, preference ONLY - never findings or computed results. Do NOT store numbers that will go stale or any personal data. "
              'Reply with JSON only: {"facts":[{"kind":"definition|preference|finding","content":"..."}]}\n\n'
              f"USER MESSAGE (only extract what the USER states): {question}")
    try:
        out = gateway.chat("memoryExtract", [{"role": "user", "content": prompt}])["content"]
        data = json.loads(out[out.index("{"): out.rindex("}") + 1])
    except Exception: return []
    return [remember(f.get("kind") if f.get("kind") in ("definition", "preference") else "definition", f["content"], thread) | {"content": f["content"]} for f in data.get("facts", [])[:2] if f.get("content")]
