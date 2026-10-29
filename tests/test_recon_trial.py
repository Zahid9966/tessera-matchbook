"""The trial kit is only worth using if the kit itself is right. These tests check the statistics for calibration, each preflight check against a
problem it claims to find, the masker's consistency guarantees, and the whole pipeline against ground truth recovered from a labelled scenario."""
import csv, hashlib, math, random
from pathlib import Path
import pytest
from tessera import warehouse
from tessera.recon import evaluate, mask_tool, public_eval, synth, trial, trial_checks, trial_stats

# ======================== statistics ========================================================================
def test_wilson_matches_published_values_and_is_sane_at_zero():
    lo, hi = trial_stats.wilson(5, 100); assert round(lo, 4) == 0.0215 and round(hi, 4) == 0.1118
    lo, hi = trial_stats.wilson(0, 50); assert lo == 0.0 and round(hi, 4) == 0.0713
    assert trial_stats.wilson(0, 0) == (0.0, 1.0)

def test_zero_event_bound_and_required_sample_size():
    for n in (10, 50, 300):
        b = trial_stats.upper_bound_zero(n); assert abs((1 - b) ** n - 0.05) < 1e-9 and b < 3 / n * 1.06
    assert trial_stats.samples_needed(0.01) == 299 and trial_stats.samples_needed(0.005) == 598
    assert (1 - 0.01) ** 299 <= 0.05 < (1 - 0.01) ** 298

@pytest.mark.parametrize("truth", [[(9000, .000), (800, .01), (200, .08)], [(5000, .002), (5000, .02)], [(20000, .0005), (300, .30)]], ids=["rare", "mixed", "tiny-bad-stratum"])
def test_stratified_interval_covers_the_truth_at_least_95_percent_of_the_time(truth):
    rng = random.Random(7); hit = trials = 0
    for _ in range(1500):
        strata, bad_total, N = [], 0, 0
        pops = []
        for Nh, p in truth:
            bad = round(Nh * p); pops.append((Nh, bad)); bad_total += bad; N += Nh
        for Nh, bad in pops:
            n = 60; idx = rng.sample(range(Nh), n); k = sum(1 for i in idx if i < bad)       # items 0..bad-1 are the wrong ones
            strata.append({"N": Nh, "n": n, "k": k})
        e = trial_stats.stratified_rate(strata); trials += 1; hit += e["low"] <= bad_total / N <= e["high"]
    assert hit / trials >= 0.95, hit / trials

# ======================== preflight ========================================================================
def _write(path, rows, delim=","):
    with open(path, "w", newline="", encoding="utf-8") as f: csv.writer(f, delimiter=delim).writerows(rows)
    return path

def _bank(n=120, start=1):
    rows, bal = [["Txn Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]], 100000.0
    for i in range(n):
        d = f"{(i % 27) + 1:02d}/{(i // 27) % 3 + start:02d}/2026"; deb = 100.5 + i if i % 2 else ""; cr = 250.25 if not i % 2 else ""
        bal += (cr or 0) - (deb or 0); rows.append([d, f"UPI/SHOP{i}/x", deb, cr, f"{bal:.2f}"])
    return rows

def codes(r): return {f["code"] for f in r["findings"] if f["severity"] != "info"}

def test_a_clean_statement_passes_with_no_warnings(tmp_path):
    r = trial_checks.preflight(_write(tmp_path / "b.csv", _bank()), "bank"); assert r["ok"] and not codes(r) - {"future_dates"}, r["findings"]

def test_each_real_world_problem_is_caught(tmp_path):
    base = _bank(); p = lambda name, rows, **kw: trial_checks.preflight(_write(tmp_path / name, rows, **kw), "bank")
    assert "preamble" in codes(p("a.csv", [["ACME BANK"], ["Account", "XXXX1234", "Period"]] + base))
    assert "footer" in codes(p("b.csv", base + [["Total"], ["computer generated"]]))
    r = p("c.csv", [base[0]] + base[1:][::-1]); assert "newest_first" in codes(r) and not r["ok"]
    sc = [r_[:] for r_ in base]; sc[5][2] = "1.234,50"; assert "decimal_comma" in codes(p("d.csv", sc))
    sy = [r_[:] for r_ in base]; sy[4][3] = "₹250.25"; assert "currency_symbol" in codes(p("e.csv", sy))
    br = [r_[:] for r_ in base]; del br[30:34]; assert "balance_breaks" in codes(p("f.csv", br))
    dup = base + [base[10]] * 4; assert "duplicate_rows" in {f["code"] for f in p("g.csv", dup)["findings"]}
    assert "delimiter" in codes(p("h.csv", base, delim=";"))
    (tmp_path / "j.csv").write_bytes("Txn Date,Narration\n01/01/2026,caf\xe9\n".encode("latin-1")); r = trial_checks.preflight(tmp_path / "j.csv", "bank"); assert not r["ok"] and r["findings"][0]["code"] == "not_utf8"

def test_ambiguous_dates_are_flagged_only_when_the_data_cannot_tell(tmp_path):
    amb = [["Txn Date", "Narration", "Withdrawal Amt.", "Deposit Amt."]] + [[f"{d:02d}/{m:02d}/2026", "x", "1.00", ""] for d in range(1, 13) for m in range(1, 13)][:60]
    r = trial_checks.preflight(_write(tmp_path / "a.csv", amb), "bank"); assert "date_ambiguous" in codes(r) and not r["ok"]
    ok = amb + [["25/03/2026", "x", "1.00", ""]]; r = trial_checks.preflight(_write(tmp_path / "b.csv", ok), "bank"); assert "date_ambiguous" not in codes(r)

def test_single_amount_layout_and_duplicate_order_ids_and_month_gaps(tmp_path):
    single = [["Date", "Description", "Amount", "Dr/Cr"]] + [[f"{d:02d}/01/2026", "x", "10.00", "Dr"] for d in range(1, 29)]
    assert "single_amount_layout" in codes(trial_checks.preflight(_write(tmp_path / "s.csv", single), "bank")) or "mapping" in codes(trial_checks.preflight(_write(tmp_path / "s.csv", single), "bank"))
    led = [["Order No", "Date", "Amount (INR)", "Pay Ref"]] + [[f"O{i % 40}", "2026-01-02", "10.00", ""] for i in range(60)]
    assert "duplicate_ids" in codes(trial_checks.preflight(_write(tmp_path / "l.csv", led), "ledger"))
    gappy = [["Txn Date", "Narration", "Withdrawal Amt.", "Deposit Amt."]] + [[f"15/{m:02d}/2026", "x", "1.00", ""] for m in (1, 2, 5, 6)] * 15
    assert "gap" in codes(trial_checks.preflight(_write(tmp_path / "g.csv", gappy), "bank"))

def test_normalize_cleans_a_copy_and_never_touches_the_original(tmp_path):
    base = _bank(); src = _write(tmp_path / "raw.csv", [["ACME BANK"], ["Account"]] + base[:1] + base[1:][::-1] + [["Total"]]); before = src.read_bytes()
    out = trial_checks.normalize(src, tmp_path / "clean.csv", reverse=True); assert out["dropped_preamble"] == 2 and out["dropped_footer"] == 1 and src.read_bytes() == before
    r = trial_checks.preflight(tmp_path / "clean.csv", "bank"); assert r["ok"] and not ({"preamble", "footer", "newest_first", "balance_breaks"} & codes(r))

# ======================== masking ==========================================================================
def test_masking_is_consistent_across_files_irreversible_without_the_key_and_leaves_amounts_alone(tmp_path):
    bank = _write(tmp_path / "bank.csv", [["Txn Date", "Narration", "Withdrawal Amt."], ["01/04/2026", "UPI/RAVI KUMAR/412345678901/ravi.k@okhdfc 9876543210", "1200.50"], ["02/04/2026", "UPI/RAVI KUMAR/512345678901/ravi.k@okhdfc", "99.00"]])
    gw = _write(tmp_path / "gw.csv", [["payment_id", "Narration", "amount"], ["pay_1", "settled UTR 412345678901", "10.00"]])
    k = tmp_path / "k.key"; a = mask_tool.mask_file(bank, tmp_path / "bank.m.csv", k, names=["Ravi Kumar"]); b = mask_tool.mask_file(gw, tmp_path / "gw.m.csv", k)
    rows = list(csv.reader(open(tmp_path / "bank.m.csv"))); txt = open(tmp_path / "bank.m.csv").read()
    for secret in ("RAVI", "412345678901", "ravi.k@okhdfc", "9876543210", "512345678901"): assert secret not in txt, secret
    assert rows[1][2] == "1200.50" and rows[1][0] == "01/04/2026"                                         # amounts and dates untouched
    assert rows[1][1].split("/")[1] == rows[2][1].split("/")[1]                                            # same payee -> same token on every row
    tok = [t for t in rows[1][1].split("/") if t.startswith("REF-")]; assert tok and tok[0] in open(tmp_path / "gw.m.csv").read()
    assert a["residual"]["clean"] and b["residual"]["clean"] and oct(k.stat().st_mode & 0o777) == "0o600" and oct((tmp_path / "bank.m.csv").stat().st_mode & 0o777) == "0o600"
    k2 = tmp_path / "k2.key"; mask_tool.mask_file(bank, tmp_path / "other.csv", k2, names=["Ravi Kumar"]); assert open(tmp_path / "other.csv").read() != txt   # different key, different tokens

def test_the_same_utr_gets_the_same_token_in_two_different_files(tmp_path):
    k = tmp_path / "k.key"; m = mask_tool.Masker(mask_tool.load_key(k))
    assert m.text("412345678901").split()[0] == m.text("x 412345678901").split()[1]

def test_residual_scan_catches_what_masking_missed():
    rows = [["UPI/ACME", "contact bob@example.com 9123456789012"]]; r = mask_tool.residual(rows, ["Narration", "Memo"]); assert not r["clean"] and r["high_severity"]["email"] and r["high_severity"]["long_digits"]

# ======================== end to end against ground truth =================================================
@pytest.fixture(scope="module")
def trialrun(tmp_path_factory):
    d = tmp_path_factory.mktemp("trial"); trial.DIR = tmp_path_factory.mktemp("out")
    synth.generate(n_orders=6000, days=240, seed=31, out=d)
    r = trial.load(str(d / "bank_statement.csv"), str(d / "gateway_settlements.csv"), str(d / "ledger.csv"), accept=True, use_llm=False); assert r["imported"], r
    return d, r

def _truth(d):
    rd = lambda f, *c: {tuple(r[x] for x in c) for r in csv.DictReader(open(d / f))}
    return {"pay": {o: p for o, p in rd("truth_order_payment.csv", "order_id", "payment_id")}, "paid": {p for _, p in rd("truth_order_payment.csv", "order_id", "payment_id")},
            "l2": rd("truth_batch_bank.csv", "settlement_id", "bank_row"), "anom": {int(r) for (r,) in rd("truth_anomalies.csv", "bank_row")}}

def test_the_control_totals_hold_exactly_on_a_full_run(trialrun):
    t = trial_checks.tieout(); assert t["all_consistent"], t["checks"]; assert t["value_weighted_match_rate"] > .9

def test_the_workbook_is_deterministic_and_stratified(trialrun):
    a = trial.build_workbook(25, seed=5); first = Path(a["path"]).read_text(); b = trial.build_workbook(25, seed=5); assert Path(b["path"]).read_text() == first
    c = trial.build_workbook(25, seed=6); assert Path(c["path"]).read_text() != first
    meta = __import__("json").loads(Path(trial.DIR / "workbook.meta.json").read_text())
    assert all(v["n"] == min(25, v["N"]) for v in meta["strata"].values()) and any(k.startswith("match:L1:A_ref") for k in meta["strata"]) and any(k.startswith("flag:") for k in meta["strata"])

def _label(trialrun, wb):
    d, _ = trialrun; T = _truth(d); rows = list(csv.DictReader(open(wb)))
    for r in rows:
        kind = r["stratum"].split(":")[0]; item = r["item"]
        if kind == "match":
            a, b = item.split("|"); ok = T["pay"].get(a) == b if r["stratum"].startswith("match:L1") else (a, b) in T["l2"]
            r["verdict"] = "correct" if ok else "wrong"
        elif kind == "exception":
            s = r["stratum"].split(":")[1]
            if s == "ledger_without_payment": r["verdict"] = "missed_match" if item in T["pay"] else "real"
            elif s == "gateway_without_order": r["verdict"] = "missed_match" if item in T["paid"] else "real"
            elif s == "batch_without_bank_credit": r["verdict"] = "missed_match" if any(a == item for a, _ in T["l2"]) else "real"
            else: r["verdict"] = "unsure"
        else: r["verdict"] = "useful" if int(item) in T["anom"] else "not_useful"
    with open(wb, "w", newline="") as f: w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

def test_estimates_from_a_labelled_sample_recover_the_known_truth(trialrun):
    d, _ = trialrun; ev = evaluate.run(d); acc = ev["L1"]["accepted"] + ev["L2"]["accepted"]; true_wrong = (ev["L1"]["wrong"] + ev["L2"]["wrong"]) / acc
    T = _truth(d); led = warehouse.query("select item_key from recon.exceptions where kind='ledger_without_payment'"); true_missed = sum(1 for r in led if r["item_key"] in T["pay"]) / len(led)
    covered = 0
    for seed in (1, 2, 3):
        trial.build_workbook(60, seed=seed); _label(trialrun, trial.DIR / "workbook.csv"); s = trial.score_workbook(trial.DIR / "workbook.csv")
        m = s["match"]; assert m["low"] <= true_wrong <= m["high"], (seed, m, true_wrong); covered += 1
        row = next(t for t in s["strata"] if t["stratum"] == "exception:ledger_without_payment")
        assert row["low"] - 1e-9 <= true_missed <= row["high"] + 1e-9, (seed, row, true_missed)
    assert covered == 3

def test_scoring_rejects_typos_reports_blanks_and_tolerates_case_and_spaces(trialrun):
    trial.build_workbook(10, seed=9); wb = trial.DIR / "workbook.csv"; rows = list(csv.DictReader(open(wb)))
    rows[0]["verdict"] = "  CORRECT "; rows[1]["verdict"] = "yes"
    with open(wb, "w", newline="") as f: w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    s = trial.score_workbook(wb); assert any("'yes'" in p for p in s["problems"]) and s["unlabelled"] > 0 and any("no verdict" in w for w in s["warnings"])

def test_the_report_states_its_limits(trialrun):
    trial.build_workbook(40, seed=2); _label(trialrun, trial.DIR / "workbook.csv"); (trial.DIR / "scores.json").write_text(__import__("json").dumps(trial.score_workbook(trial.DIR / "workbook.csv")))
    txt = trial.report(); assert "Control totals" in txt and "95% interval" in txt and "does and does not show" in txt and "by value" in txt

# ======================== public-data helpers + generator regression ====================================
def test_real_orders_flow_through_the_generator_with_a_consistent_truth(tmp_path):
    import datetime as dt
    rng = random.Random(1); orders = [(f"INV{i}", dt.date(2026, 1, 1) + dt.timedelta(days=rng.randrange(120)), rng.choice([1999, 4999, 12050, 899]) + rng.randrange(0, 5)) for i in range(800)]
    a = synth.generate(orders=orders, seed=4, out=tmp_path / "a"); synth.generate(orders=orders, seed=4, out=tmp_path / "b")
    assert a["ledger"] == 800 and (tmp_path / "a" / "ledger.csv").read_bytes() == (tmp_path / "b" / "ledger.csv").read_bytes()
    led = {r["Order No"]: r["Amount (INR)"] for r in csv.DictReader(open(tmp_path / "a" / "ledger.csv"))}; assert set(led) == {o[0] for o in orders}
    assert all(round(float(led[o]) * 100) == amt for o, _, amt in orders[:50])                      # real amounts survive exactly (integer paise)

def test_collision_statistic_and_order_building():
    import datetime as dt
    d = dt.date(2026, 3, 1); lines = [("1", d, 500), ("1", d, 250), ("2", d, 750), ("3", d + dt.timedelta(days=1), 900), ("4", d + dt.timedelta(days=30), 750)]
    allo = public_eval.orders_from(lines, "all_invoices"); one = public_eval.orders_from(lines, "single_line_invoices")
    assert {o[0]: o[2] for o in allo} == {"INV1": 750, "INV2": 750, "INV3": 900, "INV4": 750} and {o[0] for o in one} == {"INV2", "INV3", "INV4"}
    assert public_eval.collision_stats(allo)["share_with_same_amount_within_3_days"] == 0.5        # INV1 and INV2 collide; INV4 is a month away

def test_default_generator_output_is_pinned(tmp_path):
    """Guards the claim that earlier published numbers stay reproducible after generator changes."""
    synth.generate(n_orders=300, days=90, seed=3, out=tmp_path)
    assert hashlib.sha256((tmp_path / "ledger.csv").read_bytes()).hexdigest()[:16] == PINNED_LEDGER and math.isfinite(1.0)

PINNED_LEDGER = "003eed8dd6525f96"

def test_numbers_after_commas_and_periods_are_masked_and_tokens_never_look_like_leaks(tmp_path):
    from tessera.ai import masking
    m = mask_tool.Masker(mask_tool.load_key(tmp_path / "k.key"))
    for s in ("REF,412345678901 paid", "UTR.412345678901", "id:412345678901;x", "a-412345678901"):
        out = m.text(s); assert "412345678901" not in out, (s, out); assert not mask_tool.LONG.search(out) and not masking.leaks(masking.mask_text(s))
    assert m.text("amount 1234567890.50 and 12,345,678.00 on 2026-03-01") == "amount 1234567890.50 and 12,345,678.00 on 2026-03-01"      # amounts and dates survive
    for i in range(3000):                                                                              # no token may ever be mistaken for an identifier
        tok = m.tok("REF", str(400000000000 + i), 10); assert not mask_tool.LONG.search(tok) and mask_tool.TOKEN.fullmatch(tok), tok
    assert mask_tool.residual([[m.text("UPI/ACME/412345678901 x@y.com")]], ["Narration"])["high_severity"]["long_digits"] == 0

def test_sample_sizes_can_be_targeted_at_the_risky_rules(trialrun):
    r = trial.build_workbook(10, seed=4, per_stratum={"match:L1:B_amount_window": 40}); meta = r["strata"]
    assert meta["match:L1:B_amount_window"][1] == min(40, meta["match:L1:B_amount_window"][0]) and meta["match:L1:A_ref"][1] == 10

def test_masking_handles_a_title_block_and_footer_and_never_silently_does_nothing(tmp_path):
    """Regression: with a title block the first line was taken as the header, nothing was masked, and the scan reported all clear."""
    p = _write(tmp_path / "b.csv", [["ACME RETAIL - STATEMENT"], ["Account No: 123456789012", "Holder: Ravi Kumar"], ["Txn Date", "Narration", "Withdrawal Amt."],
                                    ["01/04/2026", "UPI/RAVI KUMAR/412345678901/ravi.k@okhdfc", "1200.50"], ["02/04/2026", "IMPS 9876543210 ABCDE1234F", "99.00"], ["Closing balance for account 123456789012"]])
    r = mask_tool.mask_file(p, tmp_path / "o.csv", tmp_path / "k.key", names=["Ravi Kumar"]); txt = (tmp_path / "o.csv").read_text()
    for secret in ("123456789012", "412345678901", "ravi.k@okhdfc", "9876543210", "ABCDE1234F", "RAVI KUMAR", "Ravi Kumar"): assert secret not in txt, secret
    assert r["residual"]["clean"] and r["title_lines_masked"] == 2 and r["footer_lines_masked"] == 1 and "1200.50" in txt and "01/04/2026" in txt and "Txn Date" in txt
    assert trial_checks.preflight(tmp_path / "o.csv", "bank")["rows"] == 2                      # the structure survives, so the next step still works
    nocols = _write(tmp_path / "n.csv", [["x", "y"], ["1", "2"]])
    with pytest.raises(mask_tool.MaskError): mask_tool.mask_file(nocols, tmp_path / "n.out.csv", tmp_path / "k.key")

def test_the_leftover_scan_looks_in_every_column_not_only_text_looking_ones():
    assert not mask_tool.residual([["01/04/2026", "100.00", "contact bob@example.com"]], ["Date", "Amount", "Zzz"])["clean"]

def test_separate_dr_cr_columns_are_not_mistaken_for_a_single_amount_column_and_load_maps_before_checking(tmp_path):
    rows = [["Tran Date", "Particulars", "Dr Amount", "Cr Amount", "Running Bal"]]
    for i in range(60): rows.append([f"{i % 27 + 1:02d}/01/2026", "x", "10.00", "", f"{1000 - 10 * (i + 1):.2f}"])
    r = trial_checks.preflight(_write(tmp_path / "b.csv", rows), "bank")
    assert "single_amount_layout" not in codes(r) and "mapping" in codes(r)                    # headers alone cannot resolve it
    full = trial_checks.preflight(tmp_path / "b.csv", "bank", {"date": "Tran Date", "description": "Particulars", "debit": "Dr Amount", "credit": "Cr Amount", "balance": "Running Bal"})
    assert not codes(full) - {"future_dates"}
    one = [["Date", "Description", "Amount", "Dr/Cr"]] + [["01/01/2026", "x", "10.00", "Dr"]] * 30
    assert "single_amount_layout" in codes(trial_checks.preflight(_write(tmp_path / "one.csv", one), "bank"))

def test_a_ledger_with_no_payment_reference_column_still_imports_and_matches(tmp_path):
    """Regression: an unmapped optional column became an untyped NULL (an INTEGER), and matching crashed comparing it with text payment ids."""
    from tessera.recon import importer, match
    d = tmp_path / "s"; synth.generate(n_orders=1500, days=120, seed=12, out=d)
    rows = list(csv.reader(open(d / "ledger.csv"))); drop = rows[0].index("Pay Ref")
    with open(d / "ledger.csv", "w", newline="") as f: csv.writer(f).writerows([[v for i, v in enumerate(r) if i != drop] for r in rows])
    gw = list(csv.reader(open(d / "gateway_settlements.csv"))); gdrop = [gw[0].index(c) for c in ("fee", "tax")]       # optional gateway columns absent too
    with open(d / "gateway_settlements.csv", "w", newline="") as f: csv.writer(f).writerows([[v for i, v in enumerate(r) if i not in gdrop] for r in gw])
    importer.load_all(d); out = match.run()
    assert warehouse.query("select count(*) as n from recon.match where layer='L1'")[0]["n"] > 1000 and out["exceptions"]
    assert not any(p["pass"] == "A_ref" for p in warehouse.query("select distinct pass from recon.match where layer='L1'"))     # nothing to match on, and no crash

def test_a_missed_optional_column_can_be_added_by_hand_and_is_revalidated(tmp_path):
    d = tmp_path / "s"; synth.generate(n_orders=1200, days=100, seed=14, out=d); trial.DIR = tmp_path / "o"
    args = (str(d / "bank_statement.csv"), str(d / "gateway_settlements.csv"), str(d / "ledger.csv"))
    r = trial.load(*args, accept=False, use_llm=False, mappings={"ledger": {"id": "Order No", "date": "Date", "amount": "Amount (INR)"}})
    assert "reference" not in r["files"]["ledger"]["mapping"]
    r = trial.load(*args, accept=False, use_llm=False, mappings={"ledger": {"id": "Order No", "date": "Date", "amount": "Amount (INR)"}}, overrides={"ledger": {"reference": "Pay Ref"}})
    assert r["files"]["ledger"]["mapping"]["reference"] == "Pay Ref" and not r["files"]["ledger"]["problems"]
    r = trial.load(*args, accept=False, use_llm=False, overrides={"ledger": {"reference": "Nonexistent"}}); assert r["files"]["ledger"]["problems"]            # a wrong override is caught, not imported
