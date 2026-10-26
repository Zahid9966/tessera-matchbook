"""Guardrails for model-written SQL: read-only, single statement, allow-listed schemas,
no PII columns, bounded rows. The model is never trusted to police itself."""
from __future__ import annotations
import sqlglot
from sqlglot import exp
from ..pipeline import models

ALLOWED_SCHEMAS = {"marts", "staging"}

class GuardError(ValueError): ...

def validate(sql: str, max_rows: int = 500) -> str:
    try:
        trees = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise GuardError(f"SQL parse error: {str(e)[:200]}") from e
    trees = [t for t in trees if t is not None]
    if len(trees) != 1: raise GuardError("exactly one statement is allowed")
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.With, exp.Subquery)) and not isinstance(tree, exp.Query):
        raise GuardError("only SELECT queries are allowed")
    for bad in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Copy, exp.Pragma, exp.Attach, exp.Set):
        if tree.find(bad): raise GuardError("write/DDL statements are not allowed")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    pii = models.pii_columns(); tables = {}
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Anonymous) or t.name.lower() in ctes and not t.db: continue
        schema = (t.db or "").lower()
        if schema not in ALLOWED_SCHEMAS:
            raise GuardError(f"table '{t.sql()}' must be schema-qualified in {sorted(ALLOWED_SCHEMAS)} (raw and meta are off limits)")
        tables[t.alias_or_name.lower()] = t.name.lower()
    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower().startswith(("read_", "glob", "duckdb_")): raise GuardError("file/system functions are not allowed")
    blocked = {c for t in set(tables.values()) for c in pii.get(t, set())}
    for col in tree.find_all(exp.Column):
        if col.name.lower() in blocked: raise GuardError(f"column '{col.name}' is PII and cannot be queried")
    if blocked and any(isinstance(s.parent, exp.Select) or (isinstance(s.parent, exp.Column) and isinstance(s.parent.parent, exp.Select)) for s in tree.find_all(exp.Star)):
        raise GuardError("SELECT * on a table with PII columns is not allowed; list the columns you need")
    return f"select * from ({sql.strip().rstrip(';')}) limit {int(max_rows)}"
