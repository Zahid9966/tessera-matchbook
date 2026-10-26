import json, re, pytest
from pathlib import Path
from tessera import warehouse
from tessera.ai import gateway, masking
from tessera.recon import ai_mapping, anomalies, explain, importer, layouts, synth

LONG = re.compile(r"(?<![\d.,])\d{9,}(?![\d]|[.,]\d)")

@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("ai"); synth.generate(n_orders=2500, days=240, seed=2, out=d)
    L = {x["name"]: x for x in layouts.render(d, d / "layouts")}
    return d, L

# ---- masking ---------------------------------------------------------------
def test_masking_hides_identifiers_keeps_amounts_and_dates():
    s = "UPI/ACME/4689012286 to ravi.k@okhdfc card 4111 1111 1111 1234 paid 1642064.50 and 12,345,678.00 on 2026-03-01"
    m = masking.mask_text(s)
    assert "4689012286" not in m and "ravi.k@okhdfc" not in m and "4111 1111 1111" not in m
    assert "1642064.50" in m and "12,345,678.00" in m and "2026-03-01" in m
    assert masking.leaks(m) == []

def test_strict_gateway_refuses_unmasked_prompts_before_any_network_call():
    with pytest.raises(masking.LeakError):
        list(gateway.chat_stream("flagExplain", [{"role": "user", "content": "account 123456789012 sent money"}], strict=True))

# ---- mapping proposals -----------------------------------------------------
def test_validation_rejects_swapped_debit_credit(files):
    _, L = files; x = L["bank_plain"]; good = dict(x["expected"])
    assert ai_mapping.check_mapping("bank", Path(x["path"]), good) == []
    swapped = {**good, "debit": good["credit"], "credit": good["debit"]}
    assert any("swapped" in p for p in ai_mapping.check_mapping("bank", Path(x["path"]), swapped))

def test_validation_rejects_non_unique_order_id_and_non_numeric_amount(files):
    _, L = files; x = L["ledger_so"]; p = Path(x["path"])
    assert any("not unique" in q for q in ai_mapping.check_mapping("ledger", p, {"id": x["expected"]["reference"], "date": x["expected"]["date"], "amount": x["expected"]["amount"]}))
    assert any("not numeric" in q for q in ai_mapping.check_mapping("ledger", p, {**x["expected"], "amount": x["expected"]["id"]}))

def test_validation_rejects_mixed_up_gateway_amounts(files):
    _, L = files; x = L["gw_plain"]; m = dict(x["expected"]); m["fee"], m["net"] = m["net"], m["fee"]
    assert any("mis-ordered" in q for q in ai_mapping.check_mapping("gateway", Path(x["path"]), m))

def test_llm_fills_what_rules_cannot_and_prompt_is_masked(files, monkeypatch):
    _, L = files; x = L["bank_tran"]; sent = []
    def fake(cap, messages, **kw):
        sent.extend(m["content"] for m in messages); assert kw.get("strict") is True
        return {"content": json.dumps({"debit": "Dr Amount", "credit": "Cr Amount", "balance": "Running Bal"})}
    monkeypatch.setattr(gateway, "chat", fake)
    r = ai_mapping.resolve("bank", x["path"])
    assert r["mapping"] == x["expected"] and r["problems"] == [] and r["source"] == "rules+llm" and r["needs_confirmation"]
    assert sent and not any(LONG.search(s) for s in sent)          # narrations hold 10-digit refs; none may reach the model

def test_wrong_llm_answer_is_caught_not_accepted(files, monkeypatch):
    _, L = files; x = L["bank_tran"]
    monkeypatch.setattr(gateway, "chat", lambda *a, **k: {"content": json.dumps({"debit": "Particulars", "credit": "Tran Date", "balance": "Chq/Ref No."})})
    assert ai_mapping.resolve("bank", x["path"])["problems"]        # the user is told; nothing passes silently

@pytest.mark.parametrize("reply", ["not json at all", json.dumps({"debit": "Column That Does Not Exist"}), "{}"])
def test_garbage_or_invented_columns_never_crash_or_leak_into_mapping(files, monkeypatch, reply):
    _, L = files; x = L["bank_tran"]
    monkeypatch.setattr(gateway, "chat", lambda *a, **k: {"content": reply})
    r = ai_mapping.resolve("bank", x["path"])
    assert "Column That Does Not Exist" not in r["mapping"].values() and r["problems"]

def test_model_down_degrades_to_rules_with_a_clear_problem(files, monkeypatch):
    def boom(*a, **k): raise gateway.GatewayError("cannot reach Ollama")
    monkeypatch.setattr(gateway, "chat", boom)
    r = ai_mapping.resolve("bank", files[1]["bank_tran"]["path"])
    assert r["source"] == "rules" and any("Ollama" in p for p in r["problems"])

# ---- explanations ------------------------------------------------------------
@pytest.fixture()
def flagged(files):
    d, _ = files; importer.load_all(d); anomalies.run()
    return warehouse.query("select row_id from recon.flag_ranked where rules[1]='outlier_for_payee' order by row_id limit 1")[0]["row_id"]

def _say(monkeypatch, text):
    monkeypatch.setattr(gateway, "chat", lambda *a, **k: {"content": text})

def test_default_explanation_is_deterministic_and_never_calls_the_model(flagged, monkeypatch):
    monkeypatch.setattr(gateway, "chat", lambda *a, **k: pytest.fail("model must not be called by default"))
    e = explain.explain(flagged, refresh=True); assert e["source"] == "template" and any(c.isdigit() for c in e["text"])

def test_guard_rejects_hallucinated_numbers_fraud_claims_wrong_currency_and_truncation(flagged, monkeypatch):
    ev = explain.evidence(flagged); core = ev["reasons"][0]
    good = f"{ev['payee']} was charged more than usual: {core.split(' charged ')[1].split(' (')[0]}. Check the invoice."
    for bad, why in [(good.replace(str(ev["amount"]), "99999.99"), "dropped"), (good + " This is clearly fraud.", "proven"),
                     (good.replace(str(ev["amount"]), "$" + str(ev["amount"])), "currency"), (good[:-10], "mid-sentence"),
                     (f"The shop received money from {ev['payee']}. " + good, "direction")]:
        _say(monkeypatch, bad); e = explain.explain(flagged, use_llm=True, refresh=True)
        assert e["source"] == "template" and e["rejected_reason"], (why, e)

def test_good_rewording_is_accepted_and_labelled(flagged, monkeypatch):
    ev = explain.evidence(flagged); m = re.search(r"charged ([\d.]+) vs usual ([\d.]+)", ev["reasons"][0])
    _say(monkeypatch, f"{ev['payee']} charged {m.group(1)} this time, but its usual amount is {m.group(2)}. Check this against the invoice.")
    e = explain.explain(flagged, use_llm=True, refresh=True); assert e["source"] == "llm" and e["rejected_reason"] is None

def test_unavailable_model_falls_back(flagged, monkeypatch):
    def boom(*a, **k): raise gateway.GatewayError("down")
    monkeypatch.setattr(gateway, "chat", boom)
    e = explain.explain(flagged, use_llm=True, refresh=True); assert e["source"] == "template" and "unavailable" in e["rejected_reason"]

def test_cache_never_attaches_an_old_explanation_to_a_different_statement(tmp_path_factory):
    """Regression: row ids repeat across statements, so a cache keyed by row id showed statement A's text on statement B."""
    texts = []
    for seed in (5, 6):
        d = tmp_path_factory.mktemp(f"st{seed}"); synth.generate(n_orders=1500, days=240, seed=seed, out=d); importer.load_all(d); anomalies.run()
        rid = warehouse.query("select row_id from recon.flag_ranked order by row_id limit 1")[0]["row_id"]; texts.append(explain.explain(rid)["text"])
    assert texts[0] != texts[1]

# ---- injection regression ---------------------------------------------------------------------------------------
def test_hostile_csv_header_cannot_inject_sql(tmp_path):
    """Column names come from the uploaded file. A header that closes the identifier must stay a plain name, never become SQL."""
    import csv
    evil = 'Narration") as description, (select count(*) from read_csv(\'/etc/passwd\', header=false)) as pwn, trim("x'
    p = tmp_path / "bank.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["Txn Date", evil, "Withdrawal Amt.", "Deposit Amt."]); w.writerow(["01/01/2026", "hello", "1.00", ""]); w.writerow(["02/01/2026", "world", "", "2.00"])
    warehouse.cursor().execute("create schema if not exists recon")
    m = {"date": "Txn Date", "description": evil, "debit": "Withdrawal Amt.", "credit": "Deposit Amt."}
    assert ai_mapping.check_mapping("bank", p, m) == []
    importer.load("bank", p, m)
    cols = [r["column_name"] for r in warehouse.query("select column_name from information_schema.columns where table_schema='recon' and table_name='bank'")]
    assert "pwn" not in cols and warehouse.query("select description from recon.bank order by row_id limit 1")[0]["description"] == "hello"
