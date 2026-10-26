"""A small dbt-style transformation layer: SQL files with {{ ref() }} / {{ source() }},
a dependency DAG, topological execution, and lineage."""
from __future__ import annotations
import re, time, yaml
from dataclasses import dataclass, field
from graphlib import TopologicalSorter, CycleError
from .. import config, warehouse

REF = re.compile(r"\{\{\s*ref\('([^']+)'\)\s*\}\}")
SRC = re.compile(r"\{\{\s*source\('([^']+)'\)\s*\}\}")

@dataclass
class Model:
    name: str
    layer: str
    sql: str
    description: str = ""
    materialize: str = "view"
    refs: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def schema(self) -> str: return self.layer

    def compiled(self) -> str:
        s = REF.sub(lambda m: f"{layer_of(m.group(1))}.{m.group(1)}", self.sql)
        return SRC.sub(lambda m: f"raw.{m.group(1)}", s)

_models: dict[str, Model] = {}

def layer_of(name: str) -> str:
    return _models[name].layer

def load() -> dict[str, Model]:
    _models.clear()
    for p in sorted(config.MODELS_DIR.glob("*/*.sql")):
        text = p.read_text()
        desc = re.search(r"--\s*description:\s*(.*)", text)
        mat = re.search(r"--\s*materialize:\s*(\w+)", text)
        _models[p.stem] = Model(p.stem, p.parent.name, text, desc.group(1).strip() if desc else "",
                                mat.group(1) if mat else ("view" if p.parent.name == "staging" else "table"),
                                REF.findall(text), SRC.findall(text))
    for m in _models.values():
        for r in m.refs:
            if r not in _models: raise ValueError(f"{m.name}: unknown ref {r}")
    order(_models)  # raises on cycles
    return _models

def order(models: dict[str, Model]) -> list[str]:
    try:
        return list(TopologicalSorter({n: set(m.refs) for n, m in models.items()}).static_order())
    except CycleError as e:
        raise ValueError(f"dependency cycle: {e.args[1]}") from e

def lineage() -> dict:
    ms = load()
    nodes = [{"id": f"raw.{s}", "name": s, "layer": "raw"} for s in sorted({s for m in ms.values() for s in m.sources})]
    nodes += [{"id": m.name, "name": m.name, "layer": m.layer, "description": m.description, "materialize": m.materialize} for m in ms.values()]
    edges = [{"from": f"raw.{s}", "to": m.name} for m in ms.values() for s in m.sources]
    edges += [{"from": r, "to": m.name} for m in ms.values() for r in m.refs]
    return {"nodes": nodes, "edges": edges, "order": order(ms)}

def downstream(name: str) -> list[str]:
    ms = load(); out, todo = set(), [name]
    while todo:
        cur = todo.pop()
        for m in ms.values():
            if cur in m.refs and m.name not in out: out.add(m.name); todo.append(m.name)
    return sorted(out)

def run(select: list[str] | None = None) -> list[dict]:
    ms = load(); cur = warehouse.cursor(); results = []
    names = order(ms)
    if select:
        want = set(select)
        for s in list(select): want |= set(downstream(s))
        names = [n for n in names if n in want]
    for n in names:
        m = ms[n]; t = time.time()
        cur.execute(f"create schema if not exists {m.schema}")
        cur.execute(f"create or replace {m.materialize} {m.schema}.{n} as {m.compiled()}")
        rows = cur.execute(f"select count(*) from {m.schema}.{n}").fetchone()[0]
        results.append({"model": n, "layer": m.layer, "rows": rows, "seconds": round(time.time() - t, 3)})
    return results

def schema_config() -> dict:
    p = config.MODELS_DIR / "schema.yml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}

def pii_columns() -> dict[str, set[str]]:
    cols = schema_config().get("columns", {})
    return {t: {c for c, meta in cs.items() if meta.get("pii")} for t, cs in cols.items()}
