"""Tool catalog. access="read" tools run freely; access="write" tools need an explicit human approval
(only the root agent can cause side effects). Every call is audited."""
from __future__ import annotations
import json, re
from dataclasses import dataclass
from typing import Callable
from .. import warehouse
from ..pipeline import jobs, models, quality
from . import guard, memory

@dataclass
class Tool:
    name: str; description: str; params: dict; access: str; fn: Callable[..., dict]
    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": self.params, "required": [k for k, v in self.params.items() if not v.get("optional")]}}}

TOOLS: dict[str, Tool] = {}

def tool(name, description, params, access="read"):
    def deco(fn): TOOLS[name] = Tool(name, description, params, access, fn); return fn
    return deco

def _p(t, d, **kw): return {"type": t, "description": d, **kw}

@tool("list_tables", "List warehouse tables/views (marts and staging) with row counts and descriptions.", {})
def list_tables() -> dict:
    ms = models.load()
    return {"tables": [{"table": f"{m.layer}.{m.name}", "description": m.description,
            "rows": warehouse.query(f"select count(*) c from {m.layer}.{m.name}")[0]["c"]} for m in ms.values()]}

@tool("describe_table", "Columns and types of a table, e.g. marts.fct_orders. PII columns are flagged and not queryable.",
      {"table": _p("string", "schema-qualified table name")})
def describe_table(table: str) -> dict:
    schema, _, name = table.partition(".")
    if schema not in guard.ALLOWED_SCHEMAS: return {"error": "only marts and staging are visible"}
    cols = warehouse.query("select column_name, data_type from information_schema.columns where table_schema=? and table_name=? order by ordinal_position", [schema, name])
    if not cols: return {"error": f"no such table {table}"}
    pii = models.pii_columns().get(name, set())
    sample = warehouse.query(f"select * exclude ({','.join(pii)}) from {table} limit 3") if pii else warehouse.query(f"select * from {table} limit 3")
    return {"table": table, "columns": [{**c, "pii": c["column_name"] in pii} for c in cols], "sample_rows": json.loads(json.dumps(sample, default=str))}

@tool("run_sql", "Run ONE read-only DuckDB SELECT over marts.* / staging.* tables. Returns rows. Use for every numeric answer.",
      {"sql": _p("string", "a single SELECT statement using schema-qualified tables")})
def run_sql(sql: str) -> dict:
    try: safe = guard.validate(sql, int(warehouse.setting("max_rows", "500")))
    except guard.GuardError as e: return {"error": f"blocked: {e}"}
    try:
        cur = warehouse.cursor().execute(safe); cols = [d[0] for d in cur.description]; rows = cur.fetchall()
    except Exception as e:
        hint = {}
        for tb in re.findall(r"(?:marts|staging)\.(\w+)", sql):
            hint[tb] = [r["column_name"] for r in warehouse.query("select column_name from information_schema.columns where table_name=?", [tb]) if r["column_name"] not in models.pii_columns().get(tb, set())]
        return {"error": f"SQL error: {str(e)[:250]}", "valid_columns": hint}
    return {"columns": cols, "rows": json.loads(json.dumps(rows, default=str)), "row_count": len(rows)}

@tool("get_lineage", "Upstream sources and downstream dependents of a model.", {"model": _p("string", "model name, e.g. fct_orders")})
def get_lineage(model: str) -> dict:
    ms = models.load()
    if model not in ms: return {"error": f"unknown model {model}"}
    m = ms[model]; return {"model": model, "refs": m.refs, "sources": m.sources, "downstream": models.downstream(model)}

@tool("get_data_quality", "Latest data-quality test results; failures first.", {})
def get_data_quality() -> dict:
    r = quality.latest(); return {"failing": [x for x in r if x["status"] in ("fail", "warn")], "passing": sum(x["status"] == "pass" for x in r)}

@tool("get_pipeline_runs", "Recent pipeline run history with status.", {})
def get_pipeline_runs() -> dict: return {"runs": json.loads(json.dumps(jobs.history(8), default=str))}

@tool("search_memory", "Search long-term memory for saved definitions, preferences and prior findings.", {"query": _p("string", "what to look up")})
def search_memory(query: str) -> dict: return {"memories": json.loads(json.dumps(memory.search(query), default=str))}

@tool("list_saved_metrics", "Saved named metrics (reusable SQL definitions).", {})
def list_saved_metrics() -> dict: return {"metrics": json.loads(json.dumps(warehouse.query("select name, description, sql from meta.saved_metrics"), default=str))}

@tool("save_metric", "Save a reusable named metric. Requires approval.", {"name": _p("string", "snake_case name"), "description": _p("string", "what it measures"), "sql": _p("string", "SELECT defining it")}, access="write")
def save_metric(name: str, description: str, sql: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,48}", name): return {"error": "name must be snake_case"}
    try: warehouse.cursor().execute(guard.validate(sql, 1)).fetchall()
    except guard.GuardError as e: return {"error": f"blocked: {e}"}
    except Exception as e: return {"error": f"metric SQL does not run: {str(e)[:200]}"}
    warehouse.cursor().execute("insert or replace into meta.saved_metrics values (?,?,?,?,now())", [name, description, sql, "analyst"])
    return {"saved": name}

@tool("run_pipeline_task", "Run a pipeline task: ingest, transform, quality or pipeline (all). Requires approval.",
      {"task": _p("string", "ingest | transform | quality | pipeline", enum=["ingest", "transform", "quality", "pipeline"])}, access="write")
def run_pipeline_task(task: str) -> dict:
    if task not in ("ingest", "transform", "quality", "pipeline"): return {"error": "unknown task"}
    r = jobs.run(task, trigger="agent"); return {"status": r["status"], "run_id": r.get("run_id")}

@tool("remember_note", "Store a durable note (metric definition, preference) in memory. Requires approval.",
      {"kind": _p("string", "definition | preference | finding"), "content": _p("string", "the note")}, access="write")
def remember_note(kind: str, content: str) -> dict: return memory.remember(kind, content)

def specs() -> list[dict]: return [t.spec() for t in TOOLS.values()]

def execute(name: str, args: dict) -> dict:
    t = TOOLS.get(name)
    if not t: return {"error": f"unknown tool {name}"}
    try: out = t.fn(**{k: v for k, v in (args or {}).items() if k in t.params})
    except TypeError as e: out = {"error": f"bad arguments: {e}"}
    except Exception as e: out = {"error": f"{e.__class__.__name__}: {str(e)[:200]}"}
    warehouse.audit("agent", f"tool:{name}", json.dumps(args, default=str)[:500])
    return out
