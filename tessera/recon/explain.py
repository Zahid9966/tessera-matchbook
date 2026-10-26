"""Plain-language explanations of flagged transactions.

The detector already produced the facts (rule, amounts, history). The model may only rephrase them.
Its output is accepted only if (a) every number it uses appears in the evidence, (b) it makes no claim
of fraud, and (c) it is short and plain. Otherwise a deterministic template is used instead, so the
user always gets a correct explanation, and we record which kind they got."""
from __future__ import annotations
import hashlib, json, re, time
from .. import warehouse
from ..ai import gateway, masking
from ..ai.agent import ungrounded_numbers

BANNED = re.compile(r"\b(fraud\w*|scam\w*|stolen|hack\w*|theft|criminal|definitely|certainly|proven?)\b", re.I)
RULE_ADVICE = {
    "duplicate_charge": "Check whether you meant to pay twice; if not, ask the payee for a refund.",
    "new_payee_large": "Check that you recognise this payee and that the amount is right.",
    "outlier_for_payee": "Compare with the invoice; the amount is far from this payee's usual bill.",
    "velocity": "Check whether you made all of these payments yourself.",
    "just_under_limit": "Check whether these repeated payments just under the limit are expected.",
    "subscription_drift": "Check whether the price rise was announced; you may be able to dispute or cancel.",
}

def _ensure(cur):
    # keyed by (row_id, evidence hash): row ids are only unique within one statement, so a bare row_id key would
    # attach a cached explanation to a different transaction after a new import
    cur.execute("create table if not exists recon.explanation_cache(row_id bigint, ev_hash varchar, text varchar, source varchar, model varchar, rejected_reason varchar, seconds double, primary key (row_id, ev_hash))")

def ev_hash(ev: dict) -> str:
    return hashlib.sha256(json.dumps([ev["row_id"], ev["posted_on"], ev["payee"], ev["amount"], ev["rules"], ev["reasons"]], default=str).encode()).hexdigest()[:16]

def evidence(row_id: int) -> dict | None:
    r = warehouse.query("select row_id, cast(posted_on as varchar) as posted_on, payee, round(debit_paise/100.0, 2) as amount, rules, reasons, score from recon.flag_ranked where row_id=?", [row_id])
    return r[0] if r else None

def template(ev: dict) -> str:
    advice = RULE_ADVICE.get(ev["rules"][0], "Check whether you recognise this payment.")
    return f"{ev['reasons'][0]}. {advice}"

# every flagged item is money going OUT to a payee; wording that reverses direction or renames the payee changes the meaning
WRONG_ROLE = re.compile(r"\b(received|receiving|earned|income|customers?|clients?|deposits?|refunded to you)\b", re.I)
CURRENCY = re.compile(r"[$€£]|\b(usd|dollars?|euros?|gbp)\b", re.I)
_NUM = re.compile(r"\d[\d,]*\.?\d*")

def _nums(s: str) -> set[float]:
    out = set()
    for n in _NUM.findall(s):
        try: out.add(round(float(n.replace(",", "").rstrip(".")), 2))
        except ValueError: pass
    return out

def check(text: str, ev: dict) -> str | None:
    """None if acceptable, else why it was rejected. Grounding alone is not enough (a model can reuse a real number
    in a false sentence), so the core fact must survive: every number and the payee name from the primary reason."""
    if not text.strip(): return "empty"
    core = ev["reasons"][0]
    amounts = _nums(re.sub(r"\d{4}-\d{2}-\d{2}|robust z=[\d.]+|\brow \d+|\d+(st|nd|rd|th)\b", " ", core.replace(ev["payee"] or "\0", " ")))
    missing = sorted(n for n in amounts if n >= 100 and n not in _nums(text))      # money amounts must survive; incidental stats need not
    if missing: return f"dropped core amounts: {missing[:3]}"
    if text.rstrip()[-1] not in ".!?": return "ends mid-sentence"
    if len(re.findall(r"[.!?](?:\s|$)", text)) > 4: return "too many sentences"
    if ev["payee"] and ev["payee"] in core and ev["payee"] not in text: return "dropped the payee name"
    if CURRENCY.search(text): return "wrong currency (amounts are INR)"
    if WRONG_ROLE.search(text): return "reverses direction or misnames the payee"
    if len(text) > 450: return "too long"
    if BANNED.search(text): return "asserts or implies proven wrongdoing"
    if text.lstrip().startswith(("{", "[", "#", "```")): return "not plain prose"
    facts = json.dumps(ev, default=str)
    bad = ungrounded_numbers(text, facts)
    if bad: return f"numbers not in the evidence: {bad[:3]}"
    return None

def _prompt(ev: dict) -> list[dict]:
    extra = "\n".join(f"- {r}" for r in ev["reasons"][1:])
    user = (f"Rewrite this finding from a shop owner's bank statement review in plain, calm English for the owner.\n"
            f"Finding (keep the payee name and every number exactly as written; the amounts are in rupees):\n{ev['reasons'][0]}\n"
            + (f"Also true:\n{extra}\n" if extra else "")
            + "Write 2 short sentences: the first restates the finding without changing its meaning, the second says one thing to check. "
              "Do not add any facts or numbers. Do not say it is fraud or a scam. Do not use currency symbols.")
    return [{"role": "system", "content": "You rewrite findings about bank transactions in plain, calm English without changing their meaning."}, {"role": "user", "content": masking.mask_text(user)}]

def explain(row_id: int, use_llm: bool = False, refresh: bool = False) -> dict:
    """Default is the deterministic template (always correct). use_llm=True is an opt-in rewording that must pass check();
    the result says which one you got."""
    cur = warehouse.cursor(); _ensure(cur)
    ev = evidence(row_id)
    if ev is None: raise KeyError(row_id)
    h = ev_hash(ev)
    if not refresh:
        c = warehouse.query("select * from recon.explanation_cache where row_id=? and ev_hash=? and (source='template' or ?)", [row_id, h, use_llm])
        if c: return c[0]
    text, source, why, model, t0 = template(ev), "template", None, None, time.time()
    if use_llm:
        try:
            raw = gateway.chat("flagExplain", _prompt(ev), strict=True)["content"].strip(); model = gateway.spec("flagExplain")["model"]
            why = check(raw, ev)
            if why is None: text, source = raw, "llm"
        except (gateway.GatewayError, masking.LeakError) as e: why = f"model unavailable: {e}"
    out = {"row_id": row_id, "ev_hash": h, "text": text, "source": source, "model": model, "rejected_reason": why, "seconds": round(time.time() - t0, 2)}
    cur.execute("insert or replace into recon.explanation_cache values (?,?,?,?,?,?,?)", [out[k] for k in ("row_id", "ev_hash", "text", "source", "model", "rejected_reason", "seconds")])
    return out

def explain_eval(n: int = 24, use_llm: bool = True) -> dict:
    """Run over the top-n flagged rows and report how often the model's wording was accepted, and why not."""
    per = max(1, n // 6)      # stratified: the top-scored flags are all one rule, which would hide weak spots
    rows = [r["row_id"] for r in warehouse.query("""select row_id from (select row_id, row_number() over (partition by rules[1] order by score desc, row_id) k from recon.flag_ranked) where k <= ?""", [per])]
    res = [explain(r, use_llm=use_llm, refresh=True) for r in rows]
    rej = {}
    for r in res:
        if r["rejected_reason"]: k = r["rejected_reason"].split(":")[0]; rej[k] = rej.get(k, 0) + 1
    return {"n": len(res), "llm_accepted": sum(r["source"] == "llm" for r in res), "fell_back_to_template": sum(r["source"] == "template" for r in res),
            "rejection_reasons": rej, "avg_seconds": round(sum(r["seconds"] for r in res) / max(len(res), 1), 1), "samples": res[:4]}
