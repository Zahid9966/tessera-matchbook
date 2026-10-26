"""Deterministic matching in two layers. Each pass only considers rows no earlier pass claimed.

Layer 1  ledger order  <->  gateway payment line
Layer 2  gateway settlement batch  <->  bank credit (1 batch, or a bounded combination of batches)

Accept rules are conservative on purpose: 'mutual uniqueness' means a pair is only accepted if
neither side has any other candidate. Ambiguity goes to the exception queue, never to a guess."""
from __future__ import annotations
import itertools, time
from .. import warehouse

WINDOW_DAYS = 3
COMBO_MAX = 3
COMBO_BUDGET_S = 20

def _manual(cur):
    cur.execute("create table if not exists recon.manual_link(order_id varchar, payment_id varchar, linked_at timestamp default now())")

def _setup(cur):
    _manual(cur)
    cur.execute("""create or replace table recon.match(layer varchar, pass varchar, left_key varchar, right_key varchar,
                   status varchar, diff_paise bigint, note varchar)""")
    cur.execute("""create or replace table recon.batch as select settlement_id,
                   upper(regexp_replace(settlement_id, '[^A-Za-z0-9]', '', 'g')) as norm_id,
                   min(settled_on) as settled_on, sum(net_paise) as net_paise, count(*) as n_lines
                   from recon.gateway_line group by settlement_id""")

def layer1(cur) -> None:
    ins = lambda p, sel: cur.execute(f"insert into recon.match select 'L1', '{p}', order_id, payment_id, '{'confirmed' if p == 'manual' else 'auto'}', diff, '' from ({sel})")
    free = """(select l.* from recon.ledger l where l.order_id not in (select left_key from recon.match where layer='L1')) l,
              (select g.* from recon.gateway_line g where g.payment_id not in (select right_key from recon.match where layer='L1')) g"""
    # M: links a human confirmed in review (applied first; ignored if either side no longer exists)
    ins("manual", """select l.order_id, g.payment_id, l.amount_paise - g.gross_paise as diff from recon.manual_link m
        join recon.ledger l on l.order_id = m.order_id join recon.gateway_line g on g.payment_id = m.payment_id""")
    # A1: payment reference. Take the single gateway line per reference.
    ins("A_ref", """select l.order_id, g.payment_id, l.amount_paise - g.gross_paise as diff
        from recon.ledger l join recon.gateway_line g on l.reference = g.payment_id
        qualify count(*) over (partition by g.payment_id) = 1 and count(*) over (partition by l.order_id) = 1""")
    # A2: order id carried on the gateway line.
    ins("A_order_id", f"""select l.order_id, g.payment_id, l.amount_paise - g.gross_paise as diff from {free}
        where l.order_id = g.order_id and l.amount_paise = g.gross_paise
        qualify count(*) over (partition by g.payment_id) = 1 and count(*) over (partition by l.order_id) = 1""")
    # B: same amount, gateway capture 0..3 days after the order, mutually unique.
    ins("B_amount_window", f"""select l.order_id, g.payment_id, 0 as diff from {free}
        where l.amount_paise = g.gross_paise and date_diff('day', l.order_date, g.captured_on) between 0 and {WINDOW_DAYS}
          and l.reference is null and g.order_id is null   -- a line already tied to another order/payment is not a candidate
        qualify count(*) over (partition by g.payment_id) = 1 and count(*) over (partition by l.order_id) = 1""")

def layer2(cur) -> None:
    ins = lambda p, sel: cur.execute(f"insert into recon.match select 'L2', '{p}', settlement_id, cast(row_id as varchar), status, diff, note from ({sel})")
    claimed_b = "select left_key from recon.match where layer='L2'"; claimed_k = "select right_key from recon.match where layer='L2'"
    # A: settlement id found in the bank narration.
    ins("A_settlement_id", """select b.settlement_id, k.row_id, case when k.credit_paise = b.net_paise then 'auto' else 'matched_with_difference' end as status,
        k.credit_paise - b.net_paise as diff, '' as note from recon.batch b join recon.bank k on k.settlement_ref = b.norm_id
        qualify count(*) over (partition by b.settlement_id) = 1 and count(*) over (partition by k.row_id) = 1""")
    # B: same amount, credit lands 0..3 days after settlement, mutually unique.
    ins("B_amount_window", f"""select b.settlement_id, k.row_id, 'auto' as status, 0 as diff, '' as note
        from (select * from recon.batch where settlement_id not in ({claimed_b})) b,
             (select * from recon.bank where credit_paise > 0 and cast(row_id as varchar) not in ({claimed_k})) k
        where k.credit_paise = b.net_paise and date_diff('day', b.settled_on, k.posted_on) between 0 and {WINDOW_DAYS}
        qualify count(*) over (partition by b.settlement_id) = 1 and count(*) over (partition by k.row_id) = 1""")
    # C: one credit = sum of 2..COMBO_MAX batches. Unique solution only, bounded in size and time.
    batches = cur.execute(f"select settlement_id, settled_on, net_paise from recon.batch where settlement_id not in ({claimed_b}) order by settled_on").fetchall()
    credits = cur.execute(f"select row_id, posted_on, credit_paise from recon.bank where credit_paise > 0 and cast(row_id as varchar) not in ({claimed_k})").fetchall()
    t0 = time.time(); used = set(); rows = []; gave_up = 0
    for rid, posted, amt in credits:
        if time.time() - t0 > COMBO_BUDGET_S: gave_up += 1; continue
        pool = [b for b in batches if b[0] not in used and 0 <= (posted - b[1]).days <= WINDOW_DAYS + 2]
        sols = [c for r in range(2, COMBO_MAX + 1) for c in itertools.combinations(pool, r) if sum(x[2] for x in c) == amt]
        if len(sols) == 1:
            for b in sols[0]: used.add(b[0]); rows.append(("L2", "C_combination", b[0], str(rid), "auto", 0, f"{len(sols[0])} batches paid together"))
    if rows: cur.executemany("insert into recon.match values (?,?,?,?,?,?,?)", rows)
    cur.execute("create or replace table recon.run_info as select ? as combo_gave_up", [gave_up])

def exceptions(cur) -> None:
    """Review queue. item_key is stable across re-imports (row ids are not), so a human's decision survives a re-run."""
    cur.execute("""create or replace table recon.exceptions as
      select 'ledger_without_payment' as kind, order_id as item_key, amount_paise as amount_paise, order_date as on_date, 'Order recorded in books, no gateway payment found' as detail
        from recon.ledger where order_id not in (select left_key from recon.match where layer='L1')
      union all select 'gateway_without_order', payment_id, gross_paise, captured_on, 'Payment captured at gateway, not in books'
        from recon.gateway_line where payment_id not in (select right_key from recon.match where layer='L1')
      union all select 'batch_without_bank_credit', settlement_id, net_paise, settled_on, 'Settlement batch has no matching bank credit'
        from recon.batch where settlement_id not in (select left_key from recon.match where layer='L2')
      union all select 'credit_without_batch',
             md5(cast(posted_on as varchar) || '|' || credit_paise || '|' || row_number() over (partition by posted_on, credit_paise order by row_id)),
             credit_paise, posted_on, 'Bank credit not explained by any settlement'
        from recon.bank where credit_paise > 0 and cast(row_id as varchar) not in (select right_key from recon.match where layer='L2')
      union all select 'settlement_amount_difference', left_key, diff_paise, null, 'Bank credit differs from the batch total (deduction, chargeback or fee change)'
        from recon.match where status = 'matched_with_difference'""")

def run() -> dict:
    cur = warehouse.cursor(); _setup(cur); layer1(cur); layer2(cur); exceptions(cur)
    summary = warehouse.query("select layer, pass, status, count(*) n from recon.match group by all order by 1, 2")
    return {"matches": summary, "exceptions": warehouse.query("select kind, count(*) as n from recon.exceptions group by 1 order by 1"),
            "combo_gave_up": cur.execute("select combo_gave_up from recon.run_info").fetchone()[0]}
