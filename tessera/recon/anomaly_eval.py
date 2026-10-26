"""Score anomaly flags against injected labels (row-level) and show how thresholds trade precision for recall."""
from __future__ import annotations
from pathlib import Path
from .. import warehouse
from . import anomalies

def _load_truth(cur, folder):
    cur.execute("create or replace table recon.truth_anom as select cast(bank_row as bigint) as row_id, rule from read_csv(?, header=true, all_varchar=true)", [str(Path(folder) / "truth_anomalies.csv")])

def score(folder) -> dict:
    cur = warehouse.cursor(); _load_truth(cur, folder)
    per = warehouse.query("""
        with t as (select rule, count(*) truth_n from recon.truth_anom group by 1),
             f as (select f.rule, count(*) flagged, sum((a.row_id is not null)::int) tp
                   from recon.flag f left join recon.truth_anom a on a.row_id=f.row_id and a.rule=f.rule group by 1)
        select coalesce(t.rule, f.rule) as rule, coalesce(truth_n,0) truth_n, coalesce(flagged,0) flagged, coalesce(tp,0) tp,
               round(coalesce(tp,0)*1.0/nullif(flagged,0), 3) as precision, round(coalesce(tp,0)*1.0/nullif(truth_n,0), 3) as recall
        from t full join f on t.rule=f.rule order by 1""")
    ranked = warehouse.query("""select r.row_id, r.score, (a.row_id is not null) as hit from recon.flag_ranked r left join recon.truth_anom a using (row_id) order by r.score desc, r.row_id""")
    atk = {k: round(sum(x["hit"] for x in ranked[:k]) / k, 3) for k in (20, 50, 100) if len(ranked) >= k}
    fps = warehouse.query("""select f.rule, b.payee, round(b.debit_paise/100.0,2) amount, f.reason from recon.flag f join recon.bank b using (row_id)
                             where not exists (select 1 from recon.truth_anom a where a.row_id=f.row_id and a.rule=f.rule) order by f.score desc limit 12""")
    return {"per_rule": per, "precision_at_k": atk, "rows_flagged": len(ranked), "labelled_rows": warehouse.query("select count(*) n from recon.truth_anom")[0]["n"],
            "false_positive_examples": fps}

def sweep(folder, param: str, values: list[float]) -> list[dict]:
    """Re-run the rules at several threshold values and report row-level precision/recall for the rule that owns the parameter."""
    rule = {"new_payee_quantile": "new_payee_large", "outlier_z": "outlier_for_payee", "velocity_min_new_payees": "velocity", "under_limit_min": "just_under_limit"}[param]
    out = []
    for v in values:
        anomalies.run(**{param: v}); r = next((x for x in score(folder)["per_rule"] if x["rule"] == rule), None)
        out.append({"param": param, "value": v, **{k: r[k] for k in ("flagged", "tp", "precision", "recall")}})
    anomalies.run()   # restore defaults
    return out
