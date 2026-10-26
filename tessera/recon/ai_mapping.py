"""LLM-assisted column mapping. The model only PROPOSES. Every proposal is validated against the data
(columns exist, dates/amounts parse, and semantic checks), and the user confirms it before import.
Deterministic rules run first; the model is asked only about fields the rules could not settle."""
from __future__ import annotations
import json
from pathlib import Path
from ..ai import gateway, masking
from .. import warehouse
from . import importer

FIELD_HELP = {
    "bank": {"date": "transaction/posting date", "description": "free-text narration or particulars of the transaction",
             "debit": "money going OUT of the account (withdrawals, paid out)", "credit": "money coming IN (deposits, received)",
             "balance": "running balance after the transaction"},
    "gateway": {"reference": "the payment's unique id at the gateway", "order_id": "the merchant's order id echoed by the gateway",
                "settlement_id": "payout / settlement batch id", "captured_date": "when the payment was captured",
                "settled_date": "when the money was paid out/settled", "gross": "payment amount before charges",
                "fee": "gateway fee / commission / MDR", "tax": "tax charged on the fee (GST)", "net": "amount paid out after fee and tax"},
    "ledger": {"id": "the merchant's unique order/invoice number", "date": "order or invoice date", "amount": "order total",
               "reference": "the gateway payment id recorded against the order (may be blank)"},
}
REQUIRED = importer.REQUIRED
MONEY = {"bank": ["debit", "credit", "balance"], "gateway": ["gross", "fee", "tax", "net"], "ledger": ["amount"]}
DATES = {"bank": ["date"], "gateway": ["captured_date", "settled_date"], "ledger": ["date"]}

def _cur(): return warehouse.cursor()

def _q(col: str) -> str: return '"' + col.replace('"', '""') + '"'

def _num(col: str) -> str:
    return f"try_cast(replace(replace(replace(nullif(trim({_q(col)}), ''), ',', ''), '(', '-'), ')', '') as decimal(18,2))"

def sample_rows(path: Path, n: int = 3) -> list[dict]:
    cur = _cur().execute("select * from read_csv(?, all_varchar=true, header=true) limit ?", [str(path), n])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def check_mapping(kind: str, path: Path, mapping: dict, partial: bool = False) -> list[str]:
    """Independent of whoever proposed the mapping. Returns a list of problems (empty = passes)."""
    problems = []; headers = importer.read_header(path)
    missing = [] if partial else [f for f in REQUIRED[kind] if f not in mapping]
    if missing: problems.append(f"required fields not mapped: {missing}")
    used = [c for c in mapping.values()]
    if len(used) != len(set(used)): problems.append("one column mapped to two fields")
    bad = [c for c in used if c not in headers]
    if bad: return problems + [f"unknown columns: {bad}"]
    cur = _cur(); p = str(path)
    for f in DATES[kind]:
        if f in mapping:
            try: importer.detect_date_format(path, mapping[f])
            except importer.ImportErrorMapping as e: problems.append(str(e))
    for f in MONEY[kind]:
        if f in mapping:
            n, ok = cur.execute(f"select count(*) filter (where nullif(trim({_q(mapping[f])}), '') is not null), count({_num(mapping[f])}) from read_csv(?, all_varchar=true)", [p]).fetchone()
            if n and ok / n < .95: problems.append(f"'{mapping[f]}' is not numeric ({ok}/{n} rows parse)")
    if problems: return problems
    if kind == "ledger" and "id" in mapping:       # ids must be (almost) unique
        n, u = cur.execute(f"select count(*), count(distinct {_q(mapping['id'])}) from read_csv(?, all_varchar=true)", [p]).fetchone()
        if n and u / n < .99: problems.append(f"'{mapping['id']}' is not unique ({u} distinct in {n} rows), so it cannot be the order id")
    if kind == "bank" and {"debit", "credit", "balance"} <= set(mapping):   # the balance must follow the movements; catches swapped debit/credit
        d, c, b = (_num(mapping[k]) for k in ("debit", "credit", "balance"))
        tot, good = cur.execute(f"""select count(*), count(*) filter (where abs((bal - prev) - (coalesce(cr,0) - coalesce(db,0))) < .01)
            from (select {b} bal, lag({b}) over () prev, {c} cr, {d} db from read_csv(?, all_varchar=true)) where prev is not null""", [p]).fetchone()
        if tot and good / tot < .98: problems.append(f"running balance does not follow debits/credits ({good}/{tot} rows); debit and credit may be swapped")
    if kind == "gateway" and {"gross", "fee", "tax", "net"} <= set(mapping):
        g, f_, t, n_ = (_num(mapping[k]) for k in ("gross", "fee", "tax", "net"))
        tot, good = cur.execute(f"select count(*), count(*) filter (where abs({g} - {f_} - {t} - {n_}) < .02) from read_csv(?, all_varchar=true)", [p]).fetchone()
        if tot and good / tot < .98: problems.append(f"net != gross - fee - tax on {tot - good} of {tot} rows; amount columns may be mixed up")
        else:   # the identity is symmetric in fee/net/tax, so also require the usual ordering gross >= net >= fee >= tax (tax is charged on the fee)
            tot, ordered = cur.execute(f"select count(*), count(*) filter (where {g} >= {n_} and {n_} >= {f_} and {f_} >= {t}) from read_csv(?, all_varchar=true)", [p]).fetchone()
            if tot and ordered / tot < .98: problems.append(f"amount columns look mis-ordered (expected gross >= net >= fee >= tax; holds on {ordered}/{tot} rows)")
    return problems

EXAMPLE = """Example (a different file, for format only):
Columns:
- "Posted": 03/01/2026 | 04/01/2026 | 04/01/2026
- "Info": UPI/SHOP-A/123 | NEFT-PAYROLL | CASH DEP
- "Out": 450.00 |  | 
- "In":  | 25000.00 | 900.00
Fields:
- debit: money going OUT of the account
- credit: money coming IN
Answer: {"debit": "Out", "credit": "In"}"""

def llm_propose(kind: str, headers: list[str], samples: list[dict], fixed: dict, fields: list[str]) -> tuple[dict, dict]:
    """Ask the local model for the given fields only. Plain-text prompt (small models parrot JSON input).
    Prompt is masked and sent in strict mode, so an unmasked identifier raises instead of being sent."""
    taken = set(fixed.values()); cand = [h for h in headers if h not in taken]
    cols = "\n".join(f'- "{h}": ' + " | ".join(str(r.get(h, "")) for r in samples) for h in cand)
    flds = "\n".join(f"- {f}: {FIELD_HELP[kind][f]}" for f in fields)
    user = (f"{EXAMPLE}\n\nNow the real file ({kind} export).\nColumns:\n{cols}\nFields:\n{flds}\n"
            f"Use each column at most once and write column names exactly as above. Use null when no column fits.\nAnswer:")
    msg = [{"role": "system", "content": "You map columns of a financial export to named fields. Reply with one JSON object only."},
           {"role": "user", "content": masking.mask_text(user)}]
    out = gateway.chat("mappingProposal", msg, json_mode=True, strict=True)["content"]
    try: raw = json.loads(out)
    except json.JSONDecodeError: return {}, {"error": "model returned invalid JSON", "raw_text": out[:200]}
    seen, prop = set(), {}
    for f in fields:
        v = raw.get(f) if isinstance(raw, dict) else None
        if isinstance(v, str) and v in cand and v not in seen: prop[f] = v; seen.add(v)
    return prop, {"raw": raw}

def resolve(kind: str, path: str | Path, use_llm: bool = True) -> dict:
    """Rules first; LLM only for what remains; everything validated. The caller must still show it to the user."""
    path = Path(path); headers = importer.read_header(path); rules = importer.propose_mapping(kind, headers)
    problems = check_mapping(kind, path, rules)
    if not problems: return {"mapping": rules, "source": "rules", "problems": [], "needs_confirmation": True}
    if not use_llm: return {"mapping": rules, "source": "rules", "problems": problems, "needs_confirmation": True}
    # keep only rule hits that survive their own checks (a wrong guess is worse than a missing one); ask the model about the rest
    own = check_mapping(kind, path, rules, partial=True)
    keep = {f: c for f, c in rules.items() if not any(c in pr for pr in own)}
    ask = [f for f in FIELD_HELP[kind] if f not in keep]
    try:
        prop, _ = llm_propose(kind, headers, sample_rows(path), keep, ask)
        for f in [x for x in ask if x not in prop]:                      # second chance, one field at a time
            p1, _m = llm_propose(kind, headers, sample_rows(path), {**keep, **prop}, [f]); prop.update(p1)
    except (gateway.GatewayError, masking.LeakError) as e: return {"mapping": keep, "source": "rules", "problems": problems + [str(e)], "needs_confirmation": True}
    merged = {**keep, **prop}; after = check_mapping(kind, path, merged)
    return {"mapping": merged, "source": "rules+llm" if prop else "rules", "problems": after, "needs_confirmation": True, "llm_fields": sorted(prop)}
