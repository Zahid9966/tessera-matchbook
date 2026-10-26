"""Extract/Load: raw files -> raw.* tables. Idempotent: unchanged files (by SHA-256) are skipped."""
from __future__ import annotations
import hashlib, re
from .. import config, warehouse

def _sha(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def run(force: bool = False) -> dict:
    cur = warehouse.cursor()
    out = {}
    for p in sorted(config.RAW.glob("*")):
        if p.suffix not in (".csv", ".jsonl", ".parquet"): continue
        name, sha = p.stem, _sha(p)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name):          # the file name becomes a table name: allow-list it
            out[p.name] = "skipped: file name must be lowercase letters, digits and underscores"; continue
        seen = cur.execute("select sha from meta.ingest_files where file=?", [p.name]).fetchone()
        if seen and seen[0] == sha and not force:
            out[name] = "unchanged"; continue
        reader = {".csv": "read_csv_auto(?, header=true, all_varchar=true)", ".jsonl": "read_json_auto(?)",
                  ".parquet": "read_parquet(?)"}[p.suffix]
        cur.execute(f"create or replace table raw.{name} as select *, current_timestamp as _loaded_at, ? as _source_file from {reader}",
                    [p.name, str(p)])
        n = cur.execute(f"select count(*) from raw.{name}").fetchone()[0]
        cur.execute("insert or replace into meta.ingest_files values (?,?,?,now())", [p.name, sha, n])
        out[name] = f"loaded {n} rows"
    return out
