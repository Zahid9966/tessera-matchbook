import csv, pytest
from tessera import warehouse
from tessera.recon import evaluate, importer, match, synth

@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    d = tmp_path_factory.mktemp("recon"); synth.generate(n_orders=5000, days=120, seed=3, out=d)
    imp = importer.load_all(d)
    match.run()
    return d, imp, evaluate.run(d)

def test_importer_maps_three_different_layouts_without_help(scenario):
    _, imp, _ = scenario
    assert imp["bank"]["mapping"]["debit"] == "Withdrawal Amt." and imp["gateway"]["mapping"]["settled_date"] == "settled_at"
    assert all(v["rows"] == v["source_rows"] and not v["problems"] for v in imp.values())

def test_money_is_exact_integer_paise(scenario):
    r = warehouse.query("select amount_paise from recon.ledger where order_id='ORD0000001'")[0]["amount_paise"]
    raw = next(csv.DictReader(open(scenario[0] / "ledger.csv")))["Amount (INR)"]
    assert r == round(float(raw) * 100)

def test_batch_totals_equal_sum_of_lines(scenario):
    assert warehouse.query("select count(*) c from recon.batch")[0]["c"] > 0
    assert warehouse.query("select sum(net_paise) s from recon.batch")[0]["s"] == warehouse.query("select sum(net_paise) s from recon.gateway_line")[0]["s"]

def test_layer1_accuracy_floor(scenario):
    l1 = scenario[2]["L1"]; assert l1["precision"] >= .998 and l1["recall"] >= .98

def test_layer2_exact(scenario):
    l2 = scenario[2]["L2"]; assert l2["wrong"] == 0 and l2["recall"] >= .97

def test_reference_passes_never_wrong(scenario):
    for p in scenario[2]["L1"]["per_pass"]:
        if p["pass"].startswith("A_"): assert p["precision"] == 1.0

def test_every_row_is_accounted_for(scenario):
    n = warehouse.query("select count(*) c from recon.ledger")[0]["c"]
    m = warehouse.query("select count(*) c from recon.match where layer='L1'")[0]["c"]
    e = warehouse.query("select count(*) c from recon.exceptions where kind='ledger_without_payment'")[0]["c"]
    assert m + e == n          # matched or in the exception queue, never silently dropped

def test_no_row_matched_twice(scenario):
    assert warehouse.query("select count(*) c from (select left_key from recon.match where layer='L1' group by 1 having count(*)>1)")[0]["c"] == 0
    assert warehouse.query("select count(*) c from (select right_key from recon.match where layer='L1' group by 1 having count(*)>1)")[0]["c"] == 0

def test_missing_columns_rejected(tmp_path):
    p = tmp_path / "x.csv"; p.write_text("foo,bar\n1,2\n")
    with pytest.raises(importer.ImportErrorMapping, match="could not map"): importer.load("bank", p)

def test_unparseable_dates_rejected(tmp_path):
    p = tmp_path / "b.csv"; p.write_text("Txn Date,Narration,Withdrawal Amt.,Deposit Amt.\nnot-a-date,x,1.00,\nalso bad,y,2.00,\n")
    with pytest.raises(importer.ImportErrorMapping, match="date"): importer.load("bank", p)

# ---- anomaly rules ---------------------------------------------------------
from tessera.recon import anomalies, anomaly_eval

@pytest.fixture(scope="module")
def anomaly_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("anom"); synth.generate(n_orders=4000, days=360, seed=22, out=d)
    importer.load_all(d); anomalies.run(); return d, anomaly_eval.score(d)

def test_rules_recover_injected_anomalies(anomaly_run):
    per = {p["rule"]: p for p in anomaly_run[1]["per_rule"]}
    for rule in ("duplicate_charge", "just_under_limit", "new_payee_large", "velocity", "outlier_for_payee"):
        assert per[rule]["recall"] >= .9, (rule, per[rule])

def test_hard_negatives_cost_precision(anomaly_run):
    per = {p["rule"]: p for p in anomaly_run[1]["per_rule"]}
    assert per["velocity"]["precision"] < 1.0      # the trade-fair day is a legitimate false positive; the harness must show it

def test_every_flag_explains_itself_with_numbers(anomaly_run):
    rows = warehouse.query("select reason from recon.flag")
    assert rows and all(len(r["reason"]) > 20 and any(c.isdigit() for c in r["reason"]) for r in rows)

def test_only_debits_flagged_and_scores_bounded(anomaly_run):
    assert warehouse.query("select count(*) c from recon.flag f join recon.bank b using(row_id) where b.debit_paise = 0")[0]["c"] == 0
    assert warehouse.query("select count(*) c from recon.flag where score < 0 or score > 1")[0]["c"] == 0

def test_duplicate_rule_flags_second_payment_only(anomaly_run):
    r = warehouse.query("""select f.row_id, b.payee, b.debit_paise from recon.flag f join recon.bank b using(row_id) where f.rule='duplicate_charge' limit 5""")
    for x in r:
        earlier = warehouse.query("select count(*) c from recon.bank where payee=? and debit_paise=? and row_id < ?", [x["payee"], x["debit_paise"], x["row_id"]])[0]["c"]
        assert earlier >= 1

def test_threshold_sweep_is_monotone_in_flag_count(anomaly_run):
    s = anomaly_eval.sweep(anomaly_run[0], "under_limit_min", [2, 3, 5]); assert s[0]["flagged"] >= s[1]["flagged"] >= s[2]["flagged"]
