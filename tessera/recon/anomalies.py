"""Statement anomaly rules. Every flag carries the rule, a score and the numbers behind it.
These find *unusual* and *duplicate* activity in a finished statement; they do not prove fraud.
Rules look at the whole statement period (retrospective review), not a live stream."""
from __future__ import annotations
from .. import warehouse

LIMIT_PAISE = 5_000_000          # per-transaction limit watched by the just-under rule
DEFAULTS = {"outlier_z": 4.0, "new_payee_quantile": .80, "velocity_min_new_payees": 5, "under_limit_min": 3, "drift_min_change": .10}

def run(**overrides) -> dict:
    p = {**DEFAULTS, **overrides}; cur = warehouse.cursor()
    cur.execute("create or replace table recon.flag(row_id bigint, rule varchar, score double, reason varchar)")
    cur.execute("""create or replace temp view debits as select row_id, posted_on, payee, debit_paise as amt,
                   count(*) over (partition by payee) as payee_n,
                   row_number() over (partition by payee order by row_id) as payee_seq
                   from recon.bank where debit_paise > 0""")
    ins = lambda sql: cur.execute(f"insert into recon.flag {sql}")
    ins("""select row_id, 'duplicate_charge', .80,
           'Same payee and amount as row ' || prev_row || ' on ' || prev_day || ' (' || payee || ', ' || round(amt/100.0,2) || ')'
           from (select *, lag(row_id) over w as prev_row, lag(posted_on) over w as prev_day from debits window w as (partition by payee, amt order by row_id))
           where prev_row is not null and date_diff('day', prev_day, posted_on) <= 1""")
    # threshold from *established* spend (payees seen 2+ times), so one-off anomalies cannot inflate their own yardstick
    cur.execute(f"select quantile_cont(amt, {p['new_payee_quantile']}) from debits where payee_n >= 2"); q = cur.fetchone()[0]
    ins(f"""select row_id, 'new_payee_large', .85, 'First and only payment to ' || payee || ': ' || round(amt/100.0,2) || ' is above the ' ||
           {int(p['new_payee_quantile']*100)} || 'th percentile of regular spend (' || round({q}/100.0,2) || ')'
           from debits where payee_n = 1 and amt >= {q}""")
    ins(f"""select row_id, 'outlier_for_payee', least(.95, .6 + z/40), payee || ' charged ' || round(amt/100.0,2) || ' vs usual ' || round(med/100.0,2) || ' (robust z=' || round(z,1) || ')'
           from (select *, (amt - med) / greatest(1.4826 * mad, .05 * med) as z from
                   (select *, median(abs(amt - med0)) over (partition by payee) as mad, med0 as med from
                     (select *, median(amt) over (partition by payee) as med0 from debits where payee_n >= 4)))
           where z > {p['outlier_z']}""")
    ins(f"""select row_id, 'velocity', .70, n_new || ' first-time payees on ' || posted_on || ' (this one: ' || payee || ', ' || round(amt/100.0,2) || ')'
           from (select *, count(*) over (partition by posted_on) as n_new from debits where payee_seq = 1)
           where n_new >= {p['velocity_min_new_payees']}""")
    ins(f"""select row_id, 'just_under_limit', .80, n_near || ' payments to ' || payee || ' within 2 weeks, each just under the ' || round({LIMIT_PAISE}/100.0,0) || ' limit'
           from (select *, count(*) over (partition by payee order by posted_on range between interval 7 days preceding and interval 7 days following) as n_near
                 from debits where amt >= {int(LIMIT_PAISE*.95)} and amt < {LIMIT_PAISE})
           where n_near >= {p['under_limit_min']}""")
    ins(f"""select row_id, 'subscription_drift', .60, payee || ' went from ' || round(l1/100.0,2) || ' to ' || round(amt/100.0,2) || ' (+' || round((amt*1.0/l1-1)*100) || '%) after 3 identical payments'
           from (select *, lag(amt,1) over w l1, lag(amt,2) over w l2, lag(amt,3) over w l3, lead(amt) over w nxt from debits window w as (partition by payee order by row_id))
           where l1 = l2 and l2 = l3 and amt <> l1 and abs(amt*1.0/l1 - 1) >= {p['drift_min_change']} and (nxt is null or nxt = amt)""")
    cur.execute("""create or replace table recon.flag_ranked as
        select f.row_id, b.posted_on, b.payee, b.debit_paise, list(f.rule order by f.score desc) as rules, round(least(1.0, max(f.score) + .05*(count(*)-1)), 3) as score,
               list(f.reason order by f.score desc) as reasons
        from recon.flag f join recon.bank b using (row_id) group by f.row_id, b.posted_on, b.payee, b.debit_paise order by score desc, row_id""")
    return {"params": p, "flags": warehouse.query("select rule, count(*) n from recon.flag group by 1 order by 1"),
            "rows_flagged": warehouse.query("select count(*) n from recon.flag_ranked")[0]["n"]}
