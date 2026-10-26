import pytest
from tessera import config, warehouse
from tessera.ai import agent, gateway, guard, tools
from tessera.pipeline import jobs, models, quality

def test_dag_is_topological_and_acyclic():
    ms = models.load(); order = models.order(ms)
    for n, m in ms.items():
        for r in m.refs: assert order.index(r) < order.index(n)

def test_cycle_detected():
    ms = models.load(); ms["stg_customers"].refs = ["dim_customers"]
    with pytest.raises(ValueError, match="cycle"): models.order(ms)
    models.load()

def test_ingest_is_idempotent():
    assert set(jobs.run("ingest")["detail"].split('"')[3::4]) == {"unchanged"}

def test_quality_catches_planted_orphan():
    res = {(r["model"], r["test"]): r for r in quality.run()}
    assert res[("stg_orders", "relationships")]["status"] == "fail"
    assert res[("stg_orders", "unique")]["status"] == "pass"   # dedup worked

def test_dedup_and_revenue_reconcile():
    a = warehouse.query("select sum(net_revenue) s from marts.fct_orders where status='completed'")[0]["s"]
    b = warehouse.query("select sum(revenue) s from marts.daily_revenue")[0]["s"]
    assert abs(a - b) < 0.01

@pytest.mark.parametrize("sql", [
    "drop table marts.fct_orders", "select 1; select 2", "select * from raw.orders", "select * from meta.runs",
    "select email from marts.dim_customers", "select * from marts.dim_customers", "select * from read_csv('/etc/passwd')",
    "insert into marts.daily_revenue values (1)", "copy marts.fct_orders to '/tmp/x'", "select * from orders"])
def test_guard_blocks(sql):
    with pytest.raises(guard.GuardError): guard.validate(sql)

def test_guard_allows_and_limits():
    out = guard.validate("with x as (select * from marts.daily_revenue) select order_date from x", 10)
    assert out.endswith("limit 10")
    r = tools.run_sql("select count(*) n from marts.dim_customers"); assert "error" not in r, r; assert r["rows"][0][0] > 0
    assert "error" in tools.run_sql("select full_name from marts.dim_customers")

def test_grounding_check():
    assert agent.ungrounded_numbers("Revenue was $1,234.50 in 3 months", "[1234.5]") == []
    assert agent.ungrounded_numbers("Revenue was $9,999 ", "[1234.5]") == ["9,999"]

def test_write_tool_pauses_for_approval_then_runs(monkeypatch):
    script = iter([
        {"type": "done", "content": "", "tool_calls": [{"name": "save_metric", "arguments": {"name": "rev_total", "description": "d", "sql": "select sum(revenue) r from marts.daily_revenue"}}]},
        {"type": "done", "content": "Saved.", "tool_calls": []}])
    monkeypatch.setattr(gateway, "chat_stream", lambda *a, **k: iter([next(script)]))
    monkeypatch.setattr(agent.memory, "enabled", lambda: False)
    t = agent.new_thread()
    evs = list(agent.run_turn(t, "save a metric"))
    ap = next(e for e in evs if e["type"] == "approval_required")
    assert warehouse.query("select count(*) c from meta.saved_metrics")[0]["c"] == 0   # nothing written yet
    evs2 = list(agent.decide(ap["approval_id"], True))
    assert any(e["type"] == "answer" for e in evs2)
    assert warehouse.query("select name from meta.saved_metrics")[0]["name"] == "rev_total"

def test_declined_approval_writes_nothing(monkeypatch):
    script = iter([
        {"type": "done", "content": "", "tool_calls": [{"name": "run_pipeline_task", "arguments": {"task": "transform"}}]},
        {"type": "done", "content": "Okay, not running it.", "tool_calls": []}])
    monkeypatch.setattr(gateway, "chat_stream", lambda *a, **k: iter([next(script)]))
    monkeypatch.setattr(agent.memory, "enabled", lambda: False)
    n = warehouse.query("select count(*) c from meta.runs")[0]["c"]
    ap = next(e for e in agent.run_turn(agent.new_thread(), "rebuild") if e["type"] == "approval_required")
    list(agent.decide(ap["approval_id"], False))
    assert warehouse.query("select count(*) c from meta.runs")[0]["c"] == n

def test_ingest_refuses_file_names_that_would_become_sql(tmp_path, monkeypatch):
    from tessera.pipeline import ingest
    raw = tmp_path / "raw"; raw.mkdir(); (raw / "ok_table.csv").write_text("a,b\n1,2\n"); (raw / "x; drop table raw.ok_table.csv").write_text("a,b\n1,2\n"); (raw / "Bad-Name.csv").write_text("a\n1\n")
    monkeypatch.setattr(config, "RAW", raw); out = ingest.run(force=True)
    assert out["ok_table"].startswith("loaded") and sum(v.startswith("skipped") for v in out.values()) == 2
    assert warehouse.query("select count(*) as n from raw.ok_table")[0]["n"] == 1
