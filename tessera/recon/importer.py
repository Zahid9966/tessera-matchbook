"""Statement importer: profile -> map columns -> normalise into canonical recon.* tables.

Mapping is deterministic (header synonyms + value sniffing) and returned for confirmation, so an AI
proposal (later) is just another source of the same mapping dict, validated by the same checks."""
from __future__ import annotations
import re
from pathlib import Path
from .. import warehouse

SYN = {
    "ledger": {
        "id": ["orderno", "orderid", "invoiceno", "invoice", "order"],
        "date": ["date", "orderdate", "invoicedate"],
        "amount": ["amountinr", "amount", "total", "grandtotal", "gross"],
        "reference": ["payref", "paymentid", "paymentref", "txnref", "reference", "utr"],
    },
    "gateway": {
        "reference": ["paymentid", "payid", "txnid", "paymentref"],
        "order_id": ["orderid", "orderno", "order", "receipt"],
        "settlement_id": ["settlementid", "payoutid", "batchid", "settlement"],
        "captured_date": ["capturedat", "captureddate", "createdat", "paymentdate"],
        "settled_date": ["settledat", "settledon", "settlementdate", "payoutdate"],
        "gross": ["amount", "gross", "grossamount"],
        "fee": ["fee", "fees", "commission", "mdr"],
        "tax": ["tax", "gst", "servicetax"],
        "net": ["net", "netamount", "settledamount", "payout"],
    },
    "bank": {
        "date": ["txndate", "date", "valuedate", "postingdate", "transactiondate"],
        "description": ["narration", "description", "particulars", "remarks", "details"],
        "debit": ["withdrawalamt", "withdrawal", "debit", "dr", "debitamount"],
        "credit": ["depositamt", "deposit", "credit", "cr", "creditamount"],
        "balance": ["closingbalance", "balance", "runningbalance"],
    },
}
REQUIRED = {"ledger": ["id", "date", "amount"], "gateway": ["reference", "settlement_id", "settled_date", "gross", "net"],
            "bank": ["date", "description", "debit", "credit"]}
DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d/%m/%y", "%m/%d/%Y"]

class ImportErrorMapping(ValueError): ...

def _norm(h: str) -> str: return re.sub(r"[^a-z0-9]", "", h.lower())

def read_header(path: Path) -> list[str]:
    cur = warehouse.cursor().execute("select * from read_csv(?, all_varchar=true, header=true) limit 0", [str(path)])
    return [d[0] for d in cur.description]

def propose_mapping(kind: str, headers: list[str]) -> dict:
    """Return {canonical_field: source_column}. Exact synonym match first, then substring."""
    norm = {_norm(h): h for h in headers}; out = {}; taken = set()
    for field, syns in SYN[kind].items():
        hit = next((norm[s] for s in syns if s in norm and norm[s] not in taken), None)
        if hit is None: hit = next((h for n, h in norm.items() if h not in taken and any(s in n for s in syns if len(s) > 3)), None)
        if hit: out[field] = hit; taken.add(hit)
    return out

def detect_date_format(path: Path, column: str) -> str:
    rows = warehouse.cursor().execute(f'select {_q(column)} from read_csv(?, all_varchar=true) where {_q(column)} is not null and {_q(column)} <> \'\' limit 300', [str(path)]).fetchall()
    best, score = None, 0.0
    from datetime import datetime
    for fmt in DATE_FORMATS:
        ok = 0
        for (v,) in rows:
            try: datetime.strptime(v.strip(), fmt); ok += 1
            except ValueError: pass
        if ok / max(len(rows), 1) > score: best, score = fmt, ok / max(len(rows), 1)
    if score < .95: raise ImportErrorMapping(f"column '{column}' does not parse as a date (best format {best}, {score:.0%})")
    return best

def _money(col: str) -> str:
    """Parse '1,234.50' / '(12.00)' / blank into integer paise (NULL when blank)."""
    return f"""cast(round(try_cast(replace(replace(replace(nullif(trim({_q(col)}), ''), ',', ''), '(', '-'), ')', '') as decimal(18,2)) * 100) as bigint)"""

def _q(col: str) -> str:
    """Quote an identifier. Column names come from the user's file, so a double quote inside one must be escaped."""
    return '"' + col.replace('"', '""') + '"'

def _date(col: str, fmt: str) -> str: return f"""cast(try_strptime(trim({_q(col)}), '{fmt}') as date)"""

def load(kind: str, path: str | Path, mapping: dict | None = None) -> dict:
    path = Path(path); headers = read_header(path); mapping = mapping or propose_mapping(kind, headers)
    missing = [f for f in REQUIRED[kind] if f not in mapping]
    if missing: raise ImportErrorMapping(f"{path.name}: could not map {missing}; headers were {headers}")
    for f, c in mapping.items():
        if c not in headers: raise ImportErrorMapping(f"mapping points at unknown column '{c}'")
    cur = warehouse.cursor(); p = str(path)
    src = "read_csv(?, all_varchar=true, header=true)"
    rn = "row_number() over ()"
    if kind == "ledger":
        fmt = detect_date_format(path, mapping["date"]); ref = mapping.get("reference")
        ref_sql = "nullif(trim(" + _q(ref) + "), '')" if ref else "cast(null as varchar)"      # typed: an untyped NULL is an INTEGER and breaks joins against text ids
        cur.execute(f"""create or replace table recon.ledger as select {rn} as row_id, trim({_q(mapping['id'])}) as order_id,
            {_date(mapping['date'], fmt)} as order_date, {_money(mapping['amount'])} as amount_paise,
            {ref_sql} as reference from {src}""", [p])
    elif kind == "gateway":
        sf = detect_date_format(path, mapping["settled_date"]); cf = detect_date_format(path, mapping["captured_date"]) if "captured_date" in mapping else None
        g = lambda f: ("nullif(trim(" + _q(mapping[f]) + "), '')") if f in mapping else "cast(null as varchar)"
        cur.execute(f"""create or replace table recon.gateway_line as select {rn} as row_id, {g('reference')} as payment_id, {g('order_id')} as order_id,
            {g('settlement_id')} as settlement_id, {_date(mapping['captured_date'], cf) if cf else 'cast(null as date)'} as captured_on,
            {_date(mapping['settled_date'], sf)} as settled_on, {_money(mapping['gross'])} as gross_paise,
            {_money(mapping['fee']) if 'fee' in mapping else 'cast(null as bigint)'} as fee_paise, {_money(mapping['tax']) if 'tax' in mapping else 'cast(null as bigint)'} as tax_paise,
            {_money(mapping['net'])} as net_paise from {src}""", [p])
    elif kind == "bank":
        fmt = detect_date_format(path, mapping["date"]); bal = mapping.get("balance")
        cur.execute(f"""create or replace table recon.bank as select {rn} as row_id, {_date(mapping['date'], fmt)} as posted_on,
            trim({_q(mapping['description'])}) as description, coalesce({_money(mapping['debit'])}, 0) as debit_paise,
            coalesce({_money(mapping['credit'])}, 0) as credit_paise, {_money(bal) if bal else 'cast(null as bigint)'} as balance_paise,
            upper(regexp_extract({_q(mapping['description'])}, '(SETL[_-]?[A-Z0-9]{{3,}})', 1)) as settlement_ref,
            coalesce(nullif(upper(regexp_extract({_q(mapping['description'])}, '^(?:UPI|NEFT|IMPS)/([^/]+)/', 1)), ''),
                     upper(trim(regexp_replace({_q(mapping['description'])}, '[0-9/_\\-]+', ' ', 'g')))) as payee from {src}""", [p])
    else: raise ValueError(kind)
    table = {"ledger": "recon.ledger", "gateway": "recon.gateway_line", "bank": "recon.bank"}[kind]
    n = cur.execute(f"select count(*) from {table}").fetchone()[0]
    raw_n = cur.execute("select count(*) from read_csv(?, all_varchar=true, header=true)", [p]).fetchone()[0]
    bad = _validate(kind, table)
    return {"table": table, "rows": n, "source_rows": raw_n, "mapping": mapping, "problems": bad}

def _validate(kind: str, table: str) -> list[str]:
    cur = warehouse.cursor(); bad = []
    checks = {"ledger": ["order_date is null", "amount_paise is null"], "gateway": ["settled_on is null", "gross_paise is null", "net_paise is null"],
              "bank": ["posted_on is null"]}[kind]
    for c in checks:
        n = cur.execute(f"select count(*) from {table} where {c}").fetchone()[0]
        if n: bad.append(f"{n} rows where {c}")
    if kind == "bank":  # a statement's own running balance must be consistent with its movements
        r = cur.execute("""select count(*) from (select balance_paise - lag(balance_paise) over (order by row_id) as delta, credit_paise - debit_paise as mv
                           from recon.bank where balance_paise is not null) where delta is not null and delta <> mv""").fetchone()[0]
        if r: bad.append(f"{r} rows where the running balance does not follow the debits/credits")
    return bad

def load_all(folder: str | Path) -> dict:
    folder = Path(folder); warehouse.cursor().execute("create schema if not exists recon")
    out = {}
    for kind, name in (("ledger", "ledger.csv"), ("gateway", "gateway_settlements.csv"), ("bank", "bank_statement.csv")):
        out[kind] = load(kind, folder / name)
    return out
