"""Score matches against ground truth. Precision = of what we auto-accepted, how much was right.
Recall = of what truly belongs together, how much we found. Reported per pass so weak passes are visible."""
from __future__ import annotations
from pathlib import Path
from .. import warehouse

def _truth(cur, folder: Path) -> None:
    cur.execute("create or replace table recon.truth_l1 as select * from read_csv(?, header=true, all_varchar=true)", [str(folder / "truth_order_payment.csv")])
    cur.execute("create or replace table recon.truth_l2 as select settlement_id, bank_row from read_csv(?, header=true, all_varchar=true)", [str(folder / "truth_batch_bank.csv")])

def run(folder: str | Path) -> dict:
    folder = Path(folder); cur = warehouse.cursor(); _truth(cur, folder)
    cur.execute("""create or replace table recon.scored as
        select m.layer, m.pass, m.left_key, m.right_key, m.status,
               case when m.layer='L1' then exists(select 1 from recon.truth_l1 t where t.order_id=m.left_key and t.payment_id=m.right_key)
                    else exists(select 1 from recon.truth_l2 t where t.settlement_id=m.left_key and t.bank_row=m.right_key) end as correct
        from recon.match m""")
    out = {}
    for layer, tt in (("L1", "recon.truth_l1"), ("L2", "recon.truth_l2")):
        truth_n = cur.execute(f"select count(*) from {tt}").fetchone()[0]
        per = warehouse.query("select pass, count(*) accepted, sum(correct::int) correct from recon.scored where layer=? group by 1 order by 1", [layer])
        acc = sum(p["accepted"] for p in per); ok = sum(p["correct"] for p in per)
        for p in per: p["precision"] = round(p["correct"] / p["accepted"], 4)
        out[layer] = {"truth_pairs": truth_n, "accepted": acc, "correct": ok, "wrong": acc - ok,
                      "precision": round(ok / acc, 4) if acc else None, "recall": round(ok / truth_n, 4) if truth_n else None, "per_pass": per}
    out["wrong_examples"] = warehouse.query("select layer, pass, left_key, right_key from recon.scored where not correct limit 10")
    return out
