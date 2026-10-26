"""Pre-flight checks on a raw statement/ledger/gateway file, and control-total tie-outs after import.
Real exports are messier than any generator: these checks exist to find that out BEFORE it silently skews a result."""
from __future__ import annotations
import csv, datetime as dt, hashlib, io, re
from collections import Counter
from pathlib import Path
from .. import warehouse
from . import importer

DELIMS = [",", ";", "\t", "|"]
DECIMAL_COMMA = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d{1,2}$|^-?\d+,\d{1,2}$")
SYMBOLS = re.compile(r"[₹$£€]|\b(rs\.?|inr|usd|gbp|eur)\b", re.I)
MONEY_FIELDS = {"bank": ["debit", "credit", "balance"], "gateway": ["gross", "fee", "tax", "net"], "ledger": ["amount"]}
DATE_FIELDS = {"bank": ["date"], "gateway": ["captured_date", "settled_date"], "ledger": ["date"]}

def _num(s: str) -> float | None:
    s = s.strip().replace(",", "").replace("(", "-").replace(")", "")
    try: return float(s) if s else None
    except ValueError: return None

def _sniff(lines: list[str]) -> str:
    best, score = ",", -1.0
    for d in DELIMS:
        c = Counter(l.count(d) for l in lines[:60]); mode, freq = c.most_common(1)[0]
        s = (freq / max(len(lines[:60]), 1)) * (1 if mode > 0 else 0) * (1 + min(mode, 10) / 10)
        if s > score: best, score = d, s
    return best

def read_table(path: str | Path) -> dict:
    """Parse leniently and report structure: delimiter, preamble/footer lines, header, data rows. Raises ValueError for non-UTF-8 input."""
    raw = Path(path).read_bytes()
    try: text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e: raise ValueError(f"not UTF-8 text ({e.reason}); re-export the file as UTF-8 CSV") from e
    lines = [l for l in text.splitlines() if l.strip()]
    d = _sniff(lines); rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=d))
    mode = Counter(len(r) for r in rows).most_common(1)[0][0]
    first = next((i for i, r in enumerate(rows) if len(r) == mode), 0)
    last = max((i for i, r in enumerate(rows) if len(r) == mode), default=0)
    return {"delimiter": d, "bom": raw.startswith(b"\xef\xbb\xbf"), "preamble": rows[:first], "header": rows[first], "data": [r for r in rows[first + 1:last + 1] if len(r) == mode],
            "footer": rows[last + 1:] + [r for r in rows[first + 1:last + 1] if len(r) != mode], "width": mode}

def _dates(vals: list[str]) -> dict:
    ok = {}
    for fmt in importer.DATE_FORMATS:
        n = 0
        for v in vals:
            try: dt.datetime.strptime(v.strip(), fmt); n += 1
            except ValueError: pass
        if vals and n / len(vals) >= .95: ok[fmt] = n
    return ok

def preflight(path: str | Path, kind: str, mapping: dict | None = None) -> dict:
    F = []
    def add(sev, code, msg, **extra): F.append({"severity": sev, "code": code, "message": msg, **extra})
    try: t = read_table(path)
    except ValueError as e: return {"file": str(path), "kind": kind, "ok": False, "rows": 0, "findings": [{"severity": "error", "code": "not_utf8", "message": str(e)}]}
    header, data = t["header"], t["data"]; mapping = mapping or importer.propose_mapping(kind, header); col = lambda f: header.index(mapping[f]) if f in mapping else None
    if t["bom"]: add("info", "bom", "File starts with a byte-order mark; it is harmless and will be ignored.")
    if t["delimiter"] != ",": add("warn", "delimiter", f"Fields are separated by {t['delimiter']!r}, not a comma. Check amounts for decimal commas, or run `normalize`.")
    if t["preamble"]: add("error", "preamble", f"{len(t['preamble'])} line(s) above the header (account summary, title...). Remove them or run `normalize`.", examples=[" ".join(r)[:80] for r in t["preamble"][:3]])
    if t["footer"]: add("warn", "footer", f"{len(t['footer'])} line(s) with a different shape below or among the data (totals, disclaimers). Remove them or run `normalize`.", examples=[" ".join(r)[:80] for r in t["footer"][:3]])
    missing = [f for f in importer.REQUIRED[kind] if f not in mapping]
    if missing: add("error", "mapping", f"Cannot find columns for {missing} from the headers alone. Headers: {header}. If the headers are just unfamiliar, `trial load` (or `preflight --use-llm`) proposes a mapping with the local model and checks it against the data.")
    if len(data) < 50: add("info", "small", f"Only {len(data)} data rows: any accuracy figure from this file will be very noisy.")
    # dates
    all_dates = []
    for f in DATE_FIELDS[kind]:
        c = col(f)
        if c is None: continue
        vals = [r[c] for r in data if r[c].strip()]; ok = _dates(vals)
        if len(vals) < len(data): add("error", "blank_dates", f"{len(data) - len(vals)} blank value(s) in '{mapping[f]}'.")
        if not ok: add("error", "date_format", f"'{mapping[f]}' does not parse as a date in any known format (e.g. {vals[:2]})."); continue
        slash = [f_ for f_ in ok if f_ in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y")]
        if "%d/%m/%Y" in ok and "%m/%d/%Y" in ok:
            dd = sum(1 for v in vals if v.split("/")[0].isdigit() and int(v.split("/")[0]) > 12); mm = sum(1 for v in vals if len(v.split("/")) > 1 and v.split("/")[1].isdigit() and int(v.split("/")[1]) > 12)
            if not dd and not mm: add("error", "date_ambiguous", f"'{mapping[f]}' could be day/month or month/day: every value has both parts <= 12, so 03/04/2026 may be 3 April or 4 March. The importer assumes day first. Confirm with the bank.")
            else: add("info", "date_format", f"'{mapping[f]}': {'day/month' if dd else 'month/day'} confirmed by values with a part above 12.")
        elif slash: add("info", "date_format", f"'{mapping[f]}' parses as {slash[0]}.")
        if any(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", v.strip()) for v in vals[:200]): add("warn", "two_digit_year", f"'{mapping[f]}' has 2-digit years; check the century is read correctly.")
        fmt = max(ok, key=ok.get); parsed = []
        for v in vals:
            try: parsed.append(dt.datetime.strptime(v.strip(), fmt).date())
            except ValueError: pass
        all_dates += parsed
        if f == DATE_FIELDS[kind][0] and len(parsed) > 3:
            up = sum(1 for a, b in zip(parsed, parsed[1:]) if b >= a) / (len(parsed) - 1)
            if up < .05 and kind == "bank": add("error", "newest_first", "Rows run newest-first. Balance checks and duplicate/drift rules need oldest-first. Run `normalize --reverse`.")
            elif up < .95 and kind == "bank": add("warn", "unordered", f"Rows are not in date order ({up:.0%} of neighbours ascend). Check the export.")
    if all_dates:
        lo, hi = min(all_dates), max(all_dates); months = {(d.year, d.month) for d in all_dates}; span = (hi.year - lo.year) * 12 + hi.month - lo.month + 1
        if len(months) < span: add("warn", "gap", f"No rows in {span - len(months)} month(s) between {lo} and {hi}: a statement period may be missing.")
        if hi > dt.date.today(): add("warn", "future_dates", f"Latest date {hi} is in the future (typo or wrong date format?).")
    # money
    for f in MONEY_FIELDS[kind]:
        c = col(f)
        if c is None: continue
        vals = [r[c].strip() for r in data if r[c].strip()]
        if not vals: continue
        dc = sum(1 for v in vals if DECIMAL_COMMA.match(v)); sym = sum(1 for v in vals if SYMBOLS.search(v)); bad = sum(1 for v in vals if _num(v) is None)
        if dc: add("error", "decimal_comma", f"'{mapping[f]}': {dc} value(s) look like decimal commas (e.g. '{next(v for v in vals if DECIMAL_COMMA.match(v))}'). Only dot decimals are supported.")
        if sym: add("error", "currency_symbol", f"'{mapping[f]}': {sym} value(s) contain a currency symbol or code; strip them before import.")
        if bad and not dc and not sym: add("error" if bad / len(vals) > .01 else "warn", "unparseable_amount", f"'{mapping[f]}': {bad} of {len(vals)} value(s) are not numbers (e.g. {[v for v in vals if _num(v) is None][:2]}).")
        if f in ("debit", "credit") and any((_num(v) or 0) < 0 for v in vals): add("warn", "negative_amounts", f"'{mapping[f]}' contains negative numbers; debit and credit columns are expected to be positive.")
    if kind == "bank" and col("debit") is not None and col("credit") is not None:
        both = sum(1 for r in data if (_num(r[col("debit")]) or 0) and (_num(r[col("credit")]) or 0)); none = sum(1 for r in data if not (_num(r[col("debit")]) or 0) and not (_num(r[col("credit")]) or 0))
        if both: add("warn", "both_sides", f"{both} row(s) have both a debit and a credit.")
        if none: add("info", "no_amount", f"{none} row(s) have neither (opening balance or memo rows).")
    norm = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in header]
    if kind == "bank" and not {"debit", "credit"} & set(mapping) and sum(n in ("amount", "amt", "transactionamount", "txnamount") for n in norm) == 1 and not any(w in n for n in norm for w in ("debit", "credit", "withdraw", "deposit", "paidout", "paidin")):
        add("error", "single_amount_layout", "This looks like ONE signed 'Amount' column (with a Dr/Cr marker). Only separate debit/credit columns are supported; split them in a spreadsheet first.")
    # balance continuity
    if kind == "bank" and all(col(f) is not None for f in ("debit", "credit", "balance")):
        rows = data if not any(f["code"] == "newest_first" for f in F) else data[::-1]; prev, tot, good, breaks = None, 0, 0, []
        for i, r in enumerate(rows):
            b = _num(r[col("balance")]); mv = (_num(r[col("credit")]) or 0) - (_num(r[col("debit")]) or 0)
            if b is not None and prev is not None:
                tot += 1
                if abs((b - prev) - mv) < .015: good += 1
                elif len(breaks) < 5: breaks.append({"data_row": i + 1, "expected_change": round(mv, 2), "actual_change": round(b - prev, 2)})
            if b is not None: prev = b
        if tot and good < tot:
            few = (tot - good) / tot < .05
            add("warn", "balance_breaks", (f"The running balance breaks at {tot - good} point(s) out of {tot}: " + ("rows are probably MISSING between the breaks (a missing page?), and the size of each gap is shown below." if few else
                "that many breaks usually means rows are out of order, duplicated, or the debit/credit columns are mixed up.")), examples=breaks)
        elif tot: add("info", "balance_ok", f"Running balance is consistent on {good}/{tot} rows.")
    # duplicates / ids / identity
    dups = len(data) - len({tuple(r) for r in data})
    if dups: add("warn" if dups / max(len(data), 1) > .01 else "info", "duplicate_rows", f"{dups} exact duplicate row(s) ({dups / len(data):.1%}). Genuine repeats exist in real statements, so these are flagged, not removed.")
    if kind == "ledger" and col("id") is not None:
        ids = [r[col("id")] for r in data]; d2 = len(ids) - len(set(ids))
        if d2: add("error", "duplicate_ids", f"{d2} order id(s) appear more than once; each order must be one row.")
    if kind == "gateway" and all(col(f) is not None for f in ("gross", "fee", "tax", "net")):
        c = [(_num(r[col("gross")]), _num(r[col("fee")]), _num(r[col("tax")]), _num(r[col("net")])) for r in data]; c = [x for x in c if None not in x]
        good = sum(1 for g, f_, t_, n in c if abs(g - f_ - t_ - n) < .02)
        if c and good / len(c) < .98: add("warn", "net_identity", f"net != gross - fee - tax on {len(c) - good} of {len(c)} rows (refunds, adjustments or other charges?).")
    return {"file": str(path), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "kind": kind, "rows": len(data), "columns": header, "mapping": mapping, "delimiter": t["delimiter"],
            "date_range": [str(min(all_dates)), str(max(all_dates))] if all_dates else None, "findings": F, "ok": not any(f["severity"] == "error" for f in F)}

def normalize(src: str | Path, dest: str | Path, reverse: bool = False) -> dict:
    """Write a cleaned copy: UTF-8, comma-delimited, preamble and footer removed, optionally oldest-first. Never edits the original."""
    t = read_table(src); rows = t["data"][::-1] if reverse else t["data"]
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(t["header"]); w.writerows(rows)
    return {"written": str(dest), "rows": len(rows), "dropped_preamble": len(t["preamble"]), "dropped_footer": len(t["footer"]), "reversed": reverse}

def tieout() -> dict:
    """Control totals after import and matching. The first four must hold exactly; if they do not, the engine (or the import) is wrong."""
    q = lambda s: warehouse.query(s)[0]
    L = q("select count(*) as n, coalesce(sum(amount_paise),0) as v, cast(min(order_date) as varchar) as lo, cast(max(order_date) as varchar) as hi from recon.ledger")
    G = q("select count(*) as n, coalesce(sum(gross_paise),0) as gross, coalesce(sum(fee_paise),0) as fee, coalesce(sum(tax_paise),0) as tax, coalesce(sum(net_paise),0) as net, cast(min(captured_on) as varchar) as lo, cast(max(settled_on) as varchar) as hi from recon.gateway_line")
    B = q("select count(*) as n, coalesce(sum(credit_paise),0) as credits, coalesce(sum(debit_paise),0) as debits, cast(min(posted_on) as varchar) as lo, cast(max(posted_on) as varchar) as hi from recon.bank")
    bt = q("select count(*) as n, coalesce(sum(net_paise),0) as net from recon.batch")
    ml = q("select count(*) as n, coalesce(sum(l.amount_paise),0) as v from recon.match m join recon.ledger l on l.order_id = m.left_key where m.layer='L1'")
    mg = q("select coalesce(sum(g.gross_paise),0) as v from recon.match m join recon.gateway_line g on g.payment_id = m.right_key where m.layer='L1'")
    ul = q("select count(*) as n, coalesce(sum(amount_paise),0) as v from recon.exceptions where kind='ledger_without_payment'")
    ug = q("select count(*) as n, coalesce(sum(amount_paise),0) as v from recon.exceptions where kind='gateway_without_order'")
    mb = q("select coalesce(sum(k.credit_paise),0) as v from (select distinct right_key from recon.match where layer='L2') m join recon.bank k on cast(k.row_id as varchar) = m.right_key")
    uc = q("select count(*) as n, coalesce(sum(amount_paise),0) as v from recon.exceptions where kind='credit_without_batch'")
    ub = q("select count(*) as n, coalesce(sum(amount_paise),0) as v from recon.exceptions where kind='batch_without_bank_credit'")
    chk = lambda name, a, b: {"name": name, "expected": a, "actual": b, "difference": a - b, "ok": a == b}
    checks = [chk("ledger total = matched orders + orders with no payment", L["v"], ml["v"] + ul["v"]), chk("gateway gross = matched payments + payments not in books", G["gross"], mg["v"] + ug["v"]),
              chk("settlement batches total = gateway net total", bt["net"], G["net"]), chk("bank credits = credits explained by a payout + unexplained credits", B["credits"], mb["v"] + uc["v"])]
    return {"checks": checks, "all_consistent": all(c["ok"] for c in checks),
            "value_weighted_match_rate": round(ml["v"] / L["v"], 4) if L["v"] else None, "count_match_rate": round(ml["n"] / L["n"], 4) if L["n"] else None,
            "unmatched": {"orders_no_payment": ul, "payments_not_in_books": ug, "payouts_not_in_bank": ub, "bank_credits_unexplained": uc},
            "money": {"ledger_total": L["v"], "gateway_gross": G["gross"], "gateway_fees": G["fee"], "gateway_tax": G["tax"], "gateway_net": G["net"], "bank_credits": B["credits"], "bank_debits": B["debits"],
                      "fee_rate": round((G["fee"] + G["tax"]) / G["gross"], 4) if G["gross"] else None},
            "coverage": {"ledger": [L["lo"], L["hi"]], "gateway": [G["lo"], G["hi"]], "bank": [B["lo"], B["hi"]]}, "counts": {"orders": L["n"], "payments": G["n"], "bank_rows": B["n"], "payouts": bt["n"]}}
