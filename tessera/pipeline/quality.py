"""Data-quality tests declared in models/schema.yml; results persisted in meta.quality_results."""
from __future__ import annotations
from .. import warehouse
from . import models

def _fq(name: str) -> str:
    return f"{models.layer_of(name)}.{name}"

def _check(t: dict, model: str) -> int:
    cur = warehouse.cursor(); tbl = _fq(model); col = t.get("column")
    test = t["test"]
    if test == "not_null": q = f"select count(*) from {tbl} where {col} is null"
    elif test == "unique": q = f"select count(*) from (select {col} from {tbl} where {col} is not null group by 1 having count(*)>1)"
    elif test == "non_negative": q = f"select count(*) from {tbl} where {col} < 0"
    elif test == "relationships":
        to_t, to_c = t["to"].split("."); q = f"select count(*) from {tbl} a left join {_fq(to_t)} b on a.{col}=b.{to_c} where a.{col} is not null and b.{to_c} is null"
    elif test == "row_count_min": q = f"select case when count(*) >= {int(t['value'])} then 0 else 1 end from {tbl}"
    else: raise ValueError(f"unknown test {test}")
    return cur.execute(q).fetchone()[0]

def run(run_id: int | None = None) -> list[dict]:
    models.load(); out = []
    for model, tests in models.schema_config().get("tests", {}).items():
        for t in tests:
            sev = t.get("severity", "error")
            try: failures = _check(t, model); status = "pass" if failures == 0 else ("warn" if sev == "warn" else "fail")
            except Exception as e: failures, status = -1, f"error: {e}"[:200]
            r = {"model": model, "column": t.get("column"), "test": t["test"], "severity": sev, "status": status, "failures": failures}
            out.append(r)
            warehouse.cursor().execute("insert into meta.quality_results values (?,?,?,?,?,?,?,now())",
                                       [run_id, model, r["column"], r["test"], sev, status, failures])
    return out

def latest() -> list[dict]:
    return warehouse.query("""select model, column_name as "column", test, severity, status, failures, checked_at from meta.quality_results
        qualify row_number() over (partition by model, column_name, test order by checked_at desc) = 1 order by status desc, model""")
