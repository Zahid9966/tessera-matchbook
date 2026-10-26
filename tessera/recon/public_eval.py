"""Evaluate matching on REAL order data (UCI "Online Retail II", CC BY 4.0, DOI 10.24432/C5CG6D).

What is real: order amounts, dates and how often the same amount repeats close together (the thing that makes matching ambiguous).
What is simulated: the payment gateway, settlement batches and bank statement, built from those orders with injected faults by
tessera.recon.synth, because no public dataset contains matched ledger + gateway + bank records. So this measures how the matcher copes with
a REAL amount distribution, not how it copes with a real bank's export formats."""
from __future__ import annotations
import datetime as dt, hashlib, json, platform, re, sys, tempfile, time
from collections import Counter, defaultdict
from pathlib import Path
from .. import config, warehouse
from . import evaluate, importer, match, synth

SRC = Path("data/public/online_retail_ii/online_retail_II.xlsx")

def load_lines(path: Path = SRC) -> tuple[list[tuple], dict]:
    """Read both sheets, drop the rows that appear in both (the sheets overlap by about a week), and tally what was excluded."""
    from python_calamine import CalamineWorkbook
    wb = CalamineWorkbook.from_path(str(path)); seen, lines, st = set(), [], Counter()
    for name in wb.sheet_names:
        for r in wb.get_sheet_by_name(name).to_python(skip_empty_area=False)[1:]:
            if len(r) < 8 or r[0] in (None, ""): st["blank_rows"] += 1; continue
            inv, stock, _, qty, when, price, cust, country = r[:8]
            st["rows_read"] += 1
            key = (str(inv), str(stock), qty, when, price, cust)
            if key in seen: st["duplicate_rows_across_sheets"] += 1; continue
            seen.add(key)
            if not isinstance(when, dt.datetime) or not isinstance(qty, (int, float)) or not isinstance(price, (int, float)): st["unparseable_rows"] += 1; continue
            inv = str(inv).rstrip("0").rstrip(".") if isinstance(inv, float) else str(inv)
            if inv.startswith("C"): st["cancellation_rows_excluded"] += 1; continue
            if not re.fullmatch(r"\d+", inv): st["non_sale_invoice_codes_excluded"] += 1; continue      # e.g. adjustments, postage-only codes
            if qty <= 0 or price <= 0: st["non_positive_rows_excluded"] += 1; continue
            lines.append((inv, when.date(), round(price * 100) * int(qty)))
    st["lines_kept"] = len(lines); return lines, dict(st)

def orders_from(lines: list[tuple], variant: str) -> list[tuple]:
    by = defaultdict(lambda: [None, 0, 0])                           # invoice -> [date, total_paise, n_lines]
    for inv, d, v in lines:
        o = by[inv]; o[0] = d if o[0] is None else min(o[0], d); o[1] += v; o[2] += 1
    rows = [(f"INV{inv}", o[0], o[1]) for inv, o in by.items() if o[1] > 0 and (variant == "all_invoices" or o[2] == 1)]
    return sorted(rows, key=lambda r: (r[1], r[0]))

def collision_stats(orders: list[tuple], window: int = 3) -> dict:
    """Data-only measure of how ambiguous amount-matching is: share of orders with another order of the same amount within +/- window days."""
    by = defaultdict(list)
    for _, d, a in orders: by[a].append(d.toordinal())
    clash = sum(1 for _, d, a in orders if sum(1 for x in by[a] if abs(x - d.toordinal()) <= window) > 1)
    return {"orders": len(orders), "distinct_amounts": len(by), "share_with_same_amount_within_3_days": round(clash / len(orders), 4)}

def run_scenario(orders: list[tuple], seed: int, p_missing_ref: float, p_gw_order_id: float) -> dict:
    out = Path(tempfile.mkdtemp(prefix="pubeval_"))
    synth.generate(orders=orders, seed=seed, out=out, p_missing_ref=p_missing_ref, p_gw_order_id=p_gw_order_id)
    importer.load_all(out)
    match.run()
    e = evaluate.run(out); l1, l2 = e["L1"], e["L2"]
    autos = {p["pass"]: p for p in l1["per_pass"]}
    return {"seed": seed, "p_missing_ref": p_missing_ref, "p_gw_order_id": p_gw_order_id,
            "L1_accepted": l1["accepted"], "L1_correct": l1["correct"], "L1_wrong": l1["wrong"], "L1_truth": l1["truth_pairs"], "L1_precision": l1["precision"], "L1_recall": l1["recall"],
            "L1_B_accepted": autos.get("B_amount_window", {}).get("accepted", 0), "L1_B_wrong": autos.get("B_amount_window", {}).get("accepted", 0) - autos.get("B_amount_window", {}).get("correct", 0),
            "L2_wrong": l2["wrong"], "L2_recall": l2["recall"]}

SETTINGS = [("references available (ledger 92%, gateway 70%)", .08, .7), ("fewer references (ledger 70%, gateway 40%)", .30, .4),
            ("sparse references (ledger 40%, gateway 20%)", .60, .2), ("no references at all", 1.0, 0.0)]

def main() -> int:
    seeds = [101, 102, 103]; t0 = time.time()
    lines, stats = load_lines(); sha = hashlib.sha256(SRC.read_bytes()).hexdigest()
    res = {"dataset": {"name": "Online Retail II", "doi": "10.24432/C5CG6D", "license": "CC BY 4.0", "sha256": sha, "load_stats": stats},
           "environment": {"python": platform.python_version(), "seeds": seeds}, "variants": {}}
    for variant in ("all_invoices", "single_line_invoices"):
        orders = orders_from(lines, variant); rows = []
        print(f"\n== {variant}: {len(orders):,} orders; collisions {collision_stats(orders)}", flush=True)
        for label, pm, pg in SETTINGS:
            runs = [run_scenario(orders, s, pm, pg) for s in seeds]
            tot = lambda k: sum(r[k] for r in runs)
            row = {"setting": label, "orders": len(orders), "runs": runs, "L1_accepted": tot("L1_accepted"), "L1_wrong": tot("L1_wrong"), "L1_truth": tot("L1_truth"),
                   "L1_precision_pooled": round(1 - tot("L1_wrong") / max(tot("L1_accepted"), 1), 5), "L1_recall_pooled": round(tot("L1_correct") / tot("L1_truth"), 4),
                   "L1_wrong_per_seed": [r["L1_wrong"] for r in runs], "L2_wrong_total": tot("L2_wrong")}
            rows.append(row); print(f"   {label:52s} wrong {row['L1_wrong']:>4} of {row['L1_accepted']:>7,} accepted | precision {row['L1_precision_pooled']:.4f} | recall {row['L1_recall_pooled']:.4f}", flush=True)
        res["variants"][variant] = {"collisions": collision_stats(orders), "rows": rows}
    res["seconds"] = round(time.time() - t0)
    Path("data/public/results.json").write_text(json.dumps(res, indent=2)); print(f"\nwrote data/public/results.json in {res['seconds']}s")
    return 0

if __name__ == "__main__":
    warehouse_path = Path("data/public/eval.duckdb"); warehouse_path.unlink(missing_ok=True); config.WAREHOUSE = warehouse_path; warehouse.close()
    sys.exit(main())
