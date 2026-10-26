"""Different banks/gateways/ERPs export the same facts under different headers and formats.
This renders the generated files into a set of such layouts, with the correct mapping known,
so column-mapping accuracy can be measured instead of assumed."""
from __future__ import annotations
import csv, datetime as dt
from pathlib import Path

# canonical field -> original header in the generated file
ORIG = {
    "bank": {"date": "Txn Date", "description": "Narration", "debit": "Withdrawal Amt.", "credit": "Deposit Amt.", "balance": "Closing Balance"},
    "gateway": {"reference": "payment_id", "order_id": "order_id", "settlement_id": "settlement_id", "captured_date": "captured_at",
                "settled_date": "settled_at", "gross": "amount", "fee": "fee", "tax": "tax", "net": "net"},
    "ledger": {"id": "Order No", "date": "Date", "amount": "Amount (INR)", "reference": "Pay Ref"},
}
FILE = {"bank": "bank_statement.csv", "gateway": "gateway_settlements.csv", "ledger": "ledger.csv"}
ORIG_DATE = {"bank": "%d/%m/%Y", "gateway": None, "ledger": "%Y-%m-%d"}   # gateway has ISO date and datetime columns

# (name, kind, {canonical: new header}, date_format, thousands_separators, distractor columns)
LAYOUTS = [
    ("bank_plain", "bank", dict(date="Date", description="Description", debit="Debit", credit="Credit", balance="Balance"), "%d/%m/%Y", False, []),
    ("bank_tran", "bank", dict(date="Tran Date", description="Particulars", debit="Dr Amount", credit="Cr Amount", balance="Running Bal"), "%d/%m/%Y", False, ["Chq/Ref No."]),
    ("bank_withdrawals", "bank", dict(date="Value Dt", description="Transaction Remarks", debit="Withdrawals", credit="Deposits", balance="Closing Bal"), "%d-%b-%Y", False, []),
    ("bank_moneyout", "bank", dict(date="Posting Date", description="Details", debit="Money Out", credit="Money In", balance="Available Balance"), "%Y-%m-%d", False, []),
    ("bank_caps_inr", "bank", dict(date="TXN DATE", description="NARRATION 1", debit="DEBIT (INR)", credit="CREDIT (INR)", balance="BAL (INR)"), "%d/%m/%Y", True, []),
    ("bank_paid", "bank", dict(date="Dt", description="Txn Details", debit="Paid Out", credit="Paid In", balance="Bal"), "%d/%m/%y", False, ["Branch"]),
    ("bank_flow", "bank", dict(date="Date of Transaction", description="Memo", debit="Outflow", credit="Inflow", balance="Ledger Balance"), "%d.%m.%Y".replace(".", "-"), True, ["Branch", "Chq/Ref No."]),
    ("gw_plain", "gateway", {k: v for k, v in ORIG["gateway"].items()}, None, False, []),
    ("gw_txnref", "gateway", dict(reference="txn_ref", order_id="merchant_order", settlement_id="payout_id", captured_date="created", settled_date="payout_date",
                                  gross="gross_amt", fee="commission", tax="gst", net="net_payout"), None, False, []),
    ("gw_title", "gateway", dict(reference="Transaction Id", order_id="Receipt", settlement_id="Settlement Batch", captured_date="Payment Date",
                                 settled_date="Settlement Date", gross="Amount", fee="MDR", tax="Service Tax", net="Settled Amount"), None, True, []),
    ("gw_credited", "gateway", dict(reference="pay id", order_id="order", settlement_id="utr batch", captured_date="captured on", settled_date="credited on",
                                    gross="txn amount", fee="charges", tax="tax on charges", net="amount credited"), None, False, ["Method"]),
    ("ledger_plain", "ledger", dict(ORIG["ledger"]), "%Y-%m-%d", False, []),
    ("ledger_invoice", "ledger", dict(id="Invoice #", date="Invoice Date", amount="Total", reference="Gateway Ref"), "%d/%m/%Y", True, []),
    ("ledger_so", "ledger", dict(id="SO Number", date="Booking Date", amount="Order Value", reference="Txn ID"), "%d-%m-%Y", False, ["Salesperson"]),
    ("ledger_sale", "ledger", dict(id="Ref", date="When", amount="Sale Amount", reference="Payment Reference"), "%d-%b-%Y", False, []),
]

# Written after the prompt/validation were tuned, and never used to tune them: a fair test.
HELD_OUT = [
    ("ho_bank_debits", "bank", dict(date="Transaction Date", description="Transaction Description", debit="Debits", credit="Credits", balance="Net Balance"), "%d-%m-%Y", False, []),
    ("ho_bank_amountdr", "bank", dict(date="Date", description="Remarks", debit="Amount Debited", credit="Amount Credited", balance="Balance Amount"), "%d/%m/%Y", True, ["S.No"]),
    ("ho_bank_cheque", "bank", dict(date="Value Date", description="Description of Transaction", debit="Withdrawal", credit="Deposit", balance="Balance"), "%Y-%m-%d", False, ["Cheque No"]),
    ("ho_gw_platform", "gateway", dict(reference="id", order_id="notes_order", settlement_id="batch_ref", captured_date="created_on", settled_date="settled_on",
                                       gross="paid_amount", fee="platform_fee", tax="tax_amount", net="payout_amount"), None, False, []),
    ("ho_gw_authorised", "gateway", dict(reference="Payment Reference", order_id="Order Reference", settlement_id="Payout Batch", captured_date="Authorised On",
                                         settled_date="Paid Out On", gross="Gross Value", fee="Fee Deducted", tax="GST on Fee", net="Net Value"), None, True, []),
    ("ho_ledger_doc", "ledger", dict(id="Document No", date="Posting Date", amount="Net Sales", reference="Pay Id"), "%d-%m-%Y", True, []),
]

def _parse(v: str, fmts) -> dt.datetime | None:
    for f in fmts:
        try: return dt.datetime.strptime(v, f)
        except ValueError: pass
    return None

def _money(v: str) -> str:
    try: return f"{float(v):,.2f}"
    except ValueError: return v

def render(src_dir: Path, dest_dir: Path, layouts: list | None = None) -> list[dict]:
    """Write one file per layout; return [{name, kind, path, expected}] where expected is the correct mapping."""
    dest_dir.mkdir(parents=True, exist_ok=True); out = []
    for name, kind, newh, dfmt, thousands, extras in (layouts or LAYOUTS):
        rows = list(csv.DictReader(open(src_dir / FILE[kind])))
        inv = {ORIG[kind][c]: h for c, h in newh.items()}                # original header -> new header
        date_cols = {o for c, o in ORIG[kind].items() if "date" in c or c in ("captured_date",)} if kind != "bank" else {ORIG[kind]["date"]}
        money_cols = {o for c, o in ORIG[kind].items() if c in ("debit", "credit", "balance", "amount", "gross", "fee", "tax", "net")}
        with open(dest_dir / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow([inv[o] for o in ORIG[kind].values()] + extras)
            for i, r in enumerate(rows):
                vals = []
                for o in ORIG[kind].values():
                    v = r[o]
                    if o in date_cols and dfmt and v:
                        d = _parse(v, ["%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"]); v = d.strftime(dfmt) if d else v
                    if o in money_cols and thousands and v: v = _money(v)
                    vals.append(v)
                vals += [str(1000000 + i) if "Ref" in e else ("MAIN" if e in ("Branch", "Method") else "x") for e in extras]
                w.writerow(vals)
        out.append({"name": name, "kind": kind, "path": str(dest_dir / f"{name}.csv"), "expected": dict(newh)})
    return out
