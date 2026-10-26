"""Fault-injecting generator for a merchant's payment flow, with ground truth.

Three sources, each in its own messy export format:
  ledger  (merchant books)      one row per order
  gateway (settlement report)   one row per captured payment, grouped into settlement batches, net of fees
  bank    (statement)           one credit per batch plus unrelated debits/credits
Truth files record which rows really belong together, so matching accuracy is measurable.
Money is integer paise throughout."""
from __future__ import annotations
import csv, json, random, datetime as dt
from pathlib import Path
from .. import config

CATALOG = [49900, 99900, 19900, 29900, 14900, 79900, 39900, 59900, 9900, 24900, 149900, 199900]

def _id(rng, prefix, n=8):
    return prefix + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))

def _rupees(p: int) -> str:
    return f"{p/100:.2f}"


RECURRING = [  # payee, monthly day, base paise, relative noise
    ("RENT-OFFICE", 1, 4500000, 0), ("INTERNET-ACT", 5, 149900, 0), ("AWS-INDIA", 7, 1800000, .08), ("ELECTRICITY-BESCOM", 12, 700000, .2),
    ("SALARY-ASHA", 28, 3200000, 0), ("SALARY-RAVI", 28, 2800000, 0), ("SALARY-MEERA", 28, 3600000, 0),
    ("SAAS-CRM", 3, 299900, 0), ("SAAS-CHAT", 4, 99900, 0), ("SAAS-DESIGN", 6, 199900, 0), ("SAAS-ANALYTICS", 8, 499900, 0)]
SUPPLIERS = [("SUPPLIER-ALPHA", 8000000), ("SUPPLIER-BRAVO", 12000000), ("SUPPLIER-CHARLIE", 6000000)]
ONEOFFS = ["TEA-STALL", "COURIER-QUICK", "STATIONERY-MART", "FUEL-STATION", "CAB-RIDE", "PRINT-SHOP", "SNACKS-CORNER"]
LIMIT = 5000000      # a per-transaction limit the "just under" rule watches (INR 50,000)

def _ref(r): return str(r.randrange(10**9, 10**10))
def _upi(payee, r): return f"UPI/{payee}/{_ref(r)}"

def _bank_noise(r, start, days):
    """Ordinary spending plus injected, labelled anomalies. Every label is a rule the detector should find."""
    rows = []; add = lambda d, payee, amt, label=None: rows.append({"d": d, "narr": _upi(payee, r), "debit": amt, "credit": 0, "label": label})
    months = max(2, days // 30)
    for m in range(months):
        for payee, day, base, noise in RECURRING:
            d = start + dt.timedelta(days=m * 30 + day - 1)
            add(d, payee, round(base * (1 + r.uniform(-noise, noise))))
    for payee, base in SUPPLIERS:
        for _ in range(r.randint(5, 9)):
            add(start + dt.timedelta(days=r.randrange(days)), payee, round(base * r.uniform(.6, 1.4)))
    for _ in range(int(days * .6)):
        add(start + dt.timedelta(days=r.randrange(days)), r.choice(ONEOFFS) + f"-{r.randrange(1, 40)}", r.randrange(5000, 60000))
    for payee in ("SAAS-CRM", "SAAS-CHAT", "SAAS-DESIGN"):  # subscription quietly raised ~25% from some month on
        base = next(b for p, _, b, _n in RECURRING if p == payee); from_m = r.randint(3, max(3, months - 2))
        for x in rows:
            if x["narr"].split("/")[1] == payee and not x["label"]:
                m = (x["d"] - start).days // 30
                if m >= from_m: x["debit"] = round(base * 1.25); x["label"] = "subscription_drift" if m == from_m else None
    # Hard negatives: legitimate activity that *looks* suspicious. Deliberately unlabelled, so rules that fire on it lose precision.
    for k in range(8):                                       # one-off large but legitimate: insurance, equipment, annual fees
        add(start + dt.timedelta(days=r.randrange(days)), r.choice(["INSURANCE-ANNUAL", "EQUIPMENT-VENDOR", "LICENSE-RENEWAL", "AUDIT-FEES"]) + f"-{k}", r.randrange(6000000, 11000000))
    for _ in range(3):                                       # a trade-fair day: many first-time vendors paid at once
        day = start + dt.timedelta(days=r.randrange(days))
        for _k in range(r.randint(6, 9)): add(day, f"FAIR-VENDOR-{_ref(r)[:6]}", r.randrange(20000, 150000))
    for _ in range(2):                                       # a regular supplier whose weekly invoice sits just under the limit
        payee = f"WHOLESALER-{_ref(r)[:3]}"
        day0 = r.randrange(days - 14)
        for w in range(r.randint(3, 5)): add(start + dt.timedelta(days=day0 + 3 * w), payee, r.randrange(int(LIMIT * .97), LIMIT))
    normal = [x for x in rows if not x["label"]]
    for _ in range(30):                                      # duplicate charge: same payee+amount again within a day
        b = r.choice(normal); rows.append({**b, "d": b["d"] + dt.timedelta(days=r.choice([0, 1])), "narr": _upi(b["narr"].split("/")[1], r), "label": "duplicate_charge"})
    for _ in range(25):                                      # first-ever payee, far larger than typical spend
        add(start + dt.timedelta(days=r.randrange(days)), f"QUICKFIX-{_ref(r)[:5]}", r.randrange(6000000, 12000000), "new_payee_large")
    for _ in range(6):                                       # a known payee billing several times its usual amount
        payee, _, base, _n = r.choice([x for x in RECURRING if x[0].startswith(("AWS", "ELECTRICITY"))])
        add(start + dt.timedelta(days=r.randrange(days)), payee, round(base * r.uniform(3.2, 5)), "outlier_for_payee")
    for _ in range(8):                                       # velocity: burst of small debits to many new payees in a day
        day = start + dt.timedelta(days=r.randrange(days))
        for _k in range(r.randint(6, 10)): add(day, f"MERCH-{_ref(r)[:6]}", r.randrange(49900, 299900), "velocity")
    for _ in range(8):                                       # just under a limit, repeated within a week to one payee
        day = start + dt.timedelta(days=r.randrange(days - 7)); payee = f"TRADER-{_ref(r)[:4]}"
        for _k in range(r.randint(3, 5)): add(day + dt.timedelta(days=r.randrange(7)), payee, r.randrange(int(LIMIT * .96), LIMIT), "just_under_limit")
    return rows

def generate(n_orders: int = 20000, days: int = 180, seed: int = 11, out: Path | None = None,
             p_missing_ref: float = 0.08, p_unpaid: float = 0.03, p_orphan_gw: float = 0.01,
             p_bank_no_id: float = 0.05, p_merged: float = 0.02, p_unsettled: float = 0.01,
             p_short: float = 0.01, orders: list | None = None, p_gw_order_id: float = 0.7) -> dict:
    """orders: optional real (order_id, date, amount_paise) tuples. When given they replace the invented orders; everything downstream
    (gateway, settlement, bank, faults, truth) is built the same way, so only the amount/date distribution is real."""
    rng = random.Random(seed)
    out = (out or config.DATA / "recon" / f"s{seed}_{n_orders}"); out.mkdir(parents=True, exist_ok=True)
    start = dt.date(2026, 1, 1)
    real = iter(orders) if orders is not None else None
    if orders is not None:
        n_orders = len(orders); start = min(o[1] for o in orders); days = (max(o[1] for o in orders) - start).days + 1
        real_amts = [o[2] for o in orders]

    ledger, gateway = [], []
    truth_pay = []                       # (order_id, payment_id)
    for i in range(1, n_orders + 1):
        if real is None:
            d = start + dt.timedelta(days=rng.randrange(days))
            amt = rng.choice(CATALOG) if rng.random() < .6 else rng.randrange(9900, 3000000)
            oid = f"ORD{i:07d}"
        else:
            oid, d, amt = next(real)
        pid = _id(rng, "pay_")
        ref = "" if rng.random() < p_missing_ref else pid
        ledger.append({"Order No": oid, "Date": d.isoformat(), "Amount (INR)": _rupees(amt), "Pay Ref": ref, "_amt": amt, "_d": d, "_pid": pid})
        if rng.random() < p_unpaid: continue
        cap = d + dt.timedelta(days=rng.choice([0, 0, 1]))
        gateway.append({"payment_id": pid, "order_id": oid if rng.random() < p_gw_order_id else "", "gross": amt, "captured": cap})
        truth_pay.append((oid, pid))
    for _ in range(int(n_orders * p_orphan_gw)):    # captured payments the books never recorded
        d = start + dt.timedelta(days=rng.randrange(days)); amt = rng.choice(CATALOG if orders is None else real_amts)
        gateway.append({"payment_id": _id(rng, "pay_"), "order_id": "", "gross": amt, "captured": d})

    # settlement: T+2 business days, one batch per settlement date
    def settle_day(c):
        d = c + dt.timedelta(days=2)
        while d.weekday() >= 5: d += dt.timedelta(days=1)
        return d
    batches: dict[dt.date, list] = {}
    for g in gateway:
        g["settled"] = settle_day(g["captured"]); batches.setdefault(g["settled"], []).append(g)
    sid_of = {d: f"setl_{i:05d}" for i, d in enumerate(sorted(batches), 1)}
    gw_rows = []
    for d, rows in batches.items():
        for g in rows:
            fee = round(g["gross"] * .02); tax = round(fee * .18)
            gw_rows.append({"payment_id": g["payment_id"], "order_id": g["order_id"], "settlement_id": sid_of[d],
                            "captured_at": g["captured"].isoformat() + "T10:00:00", "settled_at": d.isoformat(),
                            "amount": _rupees(g["gross"]), "fee": _rupees(fee), "tax": _rupees(tax), "net": _rupees(g["gross"] - fee - tax),
                            "_net": g["gross"] - fee - tax})
    rng.shuffle(gw_rows)
    net_of = {}
    for r in gw_rows: net_of[r["settlement_id"]] = net_of.get(r["settlement_id"], 0) + r["_net"]
    date_of = {sid: d for d, sid in sid_of.items()}  # settlement_id -> date

    # bank: one credit per batch (with faults) + noise
    ids_sorted = sorted(net_of, key=lambda s: date_of[s]); bank, truth_bank = [], []
    i = 0
    while i < len(ids_sorted):
        sid = ids_sorted[i]; r = rng.random()
        if r < p_unsettled: i += 1; continue                                     # never arrived
        members = [sid]
        if r < p_unsettled + p_merged and i + 1 < len(ids_sorted): members.append(ids_sorted[i + 1])   # two batches paid together
        amt = sum(net_of[m] for m in members)
        if rng.random() < p_short and len(members) == 1: amt -= rng.randrange(100, 20000)            # deduction/chargeback
        no_id = rng.random() < p_bank_no_id or len(members) > 1
        d = date_of[members[-1]] + dt.timedelta(days=rng.choice([0, 1, 1]))
        narr = (f"NEFT-GATEWAYPAY-{members[0].upper().replace('_','')}-ACME RETAIL" if not no_id
                else rng.choice(["NEFT-GATEWAYPAY-ACME RETAIL", "IMPS/GATEWAYPAY/PAYOUT/ACME", "NEFT CR-GATEWAY PAYMENTS PVT LTD"]))
        bank.append({"d": d, "narr": narr, "debit": 0, "credit": amt}); truth_bank.append((members, len(bank)))
        i += len(members)
    bank.extend(_bank_noise(random.Random(seed + 1000), start, days))   # separate stream: matching data unchanged
    bank_sorted = sorted(enumerate(bank), key=lambda t: (t[1]["d"], t[0]))
    row_of = {orig: pos + 1 for pos, (orig, _) in enumerate(bank_sorted)}
    bal = 10_000_000
    with open(out / "bank_statement.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["Txn Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
        for _, b in bank_sorted:
            bal += b["credit"] - b["debit"]
            w.writerow([b["d"].strftime("%d/%m/%Y"), b["narr"], _rupees(b["debit"]) if b["debit"] else "", _rupees(b["credit"]) if b["credit"] else "", _rupees(bal)])
    truth_bank_rows = [(m, row_of[idx - 1]) for members, idx in truth_bank for m in members]
    truth_anom = [(row_of[k], b["label"]) for k, b in enumerate(bank) if b.get("label")]
    with open(out / "truth_anomalies.csv", "w", newline="") as f:
        csv.writer(f).writerows([("bank_row", "rule"), *truth_anom])

    with open(out / "ledger.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["Order No", "Date", "Amount (INR)", "Pay Ref"])
        for r in ledger: w.writerow([r["Order No"], r["Date"], r["Amount (INR)"], r["Pay Ref"]])
    cols = ["payment_id", "order_id", "settlement_id", "captured_at", "settled_at", "amount", "fee", "tax", "net"]
    with open(out / "gateway_settlements.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(cols)
        for r in gw_rows: w.writerow([r[c] for c in cols])
    with open(out / "truth_order_payment.csv", "w", newline="") as f:
        csv.writer(f).writerows([("order_id", "payment_id"), *truth_pay])
    with open(out / "truth_batch_bank.csv", "w", newline="") as f:
        csv.writer(f).writerows([("settlement_id", "bank_row"), *truth_bank_rows])
    (out / "truth_meta.json").write_text(json.dumps({"seed": seed, "n_orders": n_orders, "orphan_gateway": int(n_orders * p_orphan_gw),
        "anomalies_injected": len(truth_anom), "settlement_ids": len(net_of)}))
    return {"dir": str(out), "ledger": len(ledger), "gateway": len(gw_rows), "bank": len(bank), "batches": len(net_of),
            "truth_order_payment": len(truth_pay), "truth_batch_bank": len(truth_bank_rows), "anomalies": len(truth_anom)}

if __name__ == "__main__":
    print(generate())
