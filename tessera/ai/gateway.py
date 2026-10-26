"""Model gateway. Call sites name a capability (analystChat, sqlWriter, ...); the catalog maps it to a
local Ollama model. All traffic stays on localhost; nothing is sent to third parties."""
from __future__ import annotations
import json, httpx
from .. import config
from . import masking

class GatewayError(RuntimeError): ...

def _client() -> httpx.Client:
    return httpx.Client(base_url=config.OLLAMA_URL, timeout=httpx.Timeout(600, connect=3))

def spec(capability: str) -> dict:
    if capability not in config.MODEL_CATALOG: raise GatewayError(f"unknown capability {capability}")
    return config.MODEL_CATALOG[capability]

def health() -> dict:
    try:
        with _client() as c:
            tags = [m["name"] for m in c.get("/api/tags", timeout=3).json().get("models", [])]
    except Exception as e:
        return {"ok": False, "error": f"Ollama unreachable at {config.OLLAMA_URL}: {e.__class__.__name__}", "installed": [], "catalog": config.MODEL_CATALOG}
    need = {s["model"] for s in config.MODEL_CATALOG.values()}
    missing = sorted(n for n in need if not any(t == n or t == n + ":latest" for t in tags))
    return {"ok": not missing, "installed": tags, "missing": missing, "catalog": config.MODEL_CATALOG}

def chat_stream(capability: str, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False, strict: bool = False):
    """Yields {'type':'token','text'} and finally {'type':'done','content','tool_calls'}.
    strict=True fails closed if any message still holds an unmasked identifier."""
    if strict: masking.assert_clean(messages)
    s = spec(capability)
    body = {"model": s["model"], "messages": messages, "stream": True,
            "options": {"temperature": s.get("temperature", 0.1), "num_ctx": 8192}}
    if tools: body["tools"] = tools
    if json_mode: body["format"] = "json"
    content, calls = [], []
    try:
        with _client() as c, c.stream("POST", "/api/chat", json=body) as r:
            if r.status_code != 200:
                raise GatewayError(f"Ollama {r.status_code}: {r.read().decode()[:300]}")
            for line in r.iter_lines():
                if not line: continue
                d = json.loads(line); m = d.get("message", {})
                if m.get("content"): content.append(m["content"]); yield {"type": "token", "text": m["content"]}
                for tc in m.get("tool_calls") or []: calls.append(tc["function"])
    except httpx.HTTPError as e:
        raise GatewayError(f"cannot reach Ollama: {e.__class__.__name__}") from e
    yield {"type": "done", "content": "".join(content), "tool_calls": calls}

def chat(capability: str, messages: list[dict], tools=None, **kw) -> dict:
    last = {}
    for ev in chat_stream(capability, messages, tools, **kw):
        if ev["type"] == "done": last = ev
    return last

def embed(texts: list[str]) -> list[list[float]]:
    s = spec("embeddings")
    try:
        with _client() as c:
            r = c.post("/api/embed", json={"model": s["model"], "input": texts})
            r.raise_for_status()
            return r.json()["embeddings"]
    except httpx.HTTPError as e:
        raise GatewayError(f"embedding failed: {e.__class__.__name__}") from e
