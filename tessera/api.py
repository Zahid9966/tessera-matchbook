from __future__ import annotations
import json, threading
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from . import warehouse
from .ai import agent, gateway, memory, tools
from .pipeline import jobs, models, quality

WEB = Path(__file__).parent / "web"

class Ask(BaseModel):
    thread_id: int | None = None
    message: str

class Decision(BaseModel):
    approve: bool

class SQL(BaseModel):
    sql: str

class Setting(BaseModel):
    value: str

def _sse(events):
    def gen():
        for ev in events: yield f"data: {json.dumps(ev, default=str)}\n\n"
        yield "data: {\"type\":\"end\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

def create_app(schedule_seconds: int = 0) -> FastAPI:
    app = FastAPI(title="Tessera")
    warehouse.connect(); models.load()
    if schedule_seconds: jobs.Scheduler({"pipeline": schedule_seconds}).start()

    @app.get("/")
    def index(): return FileResponse(WEB / "index.html")

    @app.get("/api/health")
    def health(): return {"warehouse": True, "llm": gateway.health()}

    # ---- pipeline & lineage
    @app.get("/api/lineage")
    def lineage(): return models.lineage()

    @app.get("/api/runs")
    def runs(): return json.loads(json.dumps(jobs.history(), default=str))

    @app.post("/api/tasks/{name}")
    def run_task(name: str):
        if name not in jobs.TASKS: raise HTTPException(404, "unknown task")
        threading.Thread(target=jobs.run, args=(name, "ui"), daemon=True).start()
        return {"started": name}

    @app.get("/api/quality")
    def qual(): return json.loads(json.dumps(quality.latest(), default=str))

    # ---- catalog & SQL workbench
    @app.get("/api/tables")
    def tables(): return tools.list_tables()

    @app.get("/api/tables/{schema}/{name}")
    def table(schema: str, name: str): return tools.describe_table(f"{schema}.{name}")

    @app.post("/api/sql")
    def sql(body: SQL): return tools.run_sql(body.sql)

    @app.get("/api/metrics")
    def metrics(): return json.loads(json.dumps(warehouse.query("select * from meta.saved_metrics order by name"), default=str))

    @app.get("/api/overview")
    def overview():
        q = lambda s: warehouse.query(s)
        return json.loads(json.dumps({
            "kpis": q("""select sum(revenue) revenue, sum(orders) orders, round(sum(revenue)/sum(orders),2) aov, sum(gross_profit) gross_profit
                         from marts.daily_revenue where order_date >= (select max(order_date) from marts.daily_revenue) - 89"""),
            "daily": q("select strftime(order_date,'%Y-%m-%d') d, revenue from marts.daily_revenue where order_date >= (select max(order_date) from marts.daily_revenue) - 89 order by 1"),
            "categories": q("select category, round(sum(net_revenue)) revenue from marts.fct_order_items where status='completed' group by 1 order by 2 desc"),
            "channels": q("select acquisition_channel, customers, conversion, avg_ltv from marts.channel_performance order by avg_ltv desc"),
            "segments": q("select segment, count(*) n from marts.dim_customers group by 1 order by 2 desc"),
            "funnel": q("select sum(viewers) viewers, sum(carters) carters, sum(checkouts) checkouts, sum(purchasers) purchasers from marts.funnel_daily"),
        }, default=str))

    # ---- agent
    @app.post("/api/chat")
    def chat(body: Ask):
        tid = body.thread_id or agent.new_thread()
        def events():
            yield {"type": "thread", "thread_id": tid}
            yield from agent.run_turn(tid, body.message)
        return _sse(events())

    @app.post("/api/approvals/{aid}")
    def approve(aid: int, body: Decision): return _sse(agent.decide(aid, body.approve))

    @app.get("/api/threads")
    def threads(): return json.loads(json.dumps(warehouse.query("select * from meta.threads order by id desc limit 30"), default=str))

    @app.get("/api/threads/{tid}")
    def thread(tid: int):
        return json.loads(json.dumps(warehouse.query("select role, content, tool_calls from meta.messages where thread_id=? and role in ('user','assistant') order by id", [tid]), default=str))

    # ---- memory & privacy controls
    @app.get("/api/memory")
    def mem(): return {"enabled": memory.enabled(), "items": json.loads(json.dumps(memory.list_all(), default=str))}

    @app.delete("/api/memory/{mid}")
    def mem_del(mid: int): memory.delete(mid); return {"deleted": mid}

    @app.put("/api/settings/{key}")
    def set_setting(key: str, body: Setting):
        if key not in ("memory_enabled", "max_rows"): raise HTTPException(400, "unknown setting")
        warehouse.cursor().execute("insert or replace into meta.memory_settings values (?,?)", [key, body.value]); warehouse.audit("user", "setting", f"{key}={body.value}")
        return {key: body.value}

    @app.get("/api/audit")
    def audit(): return json.loads(json.dumps(warehouse.query("select ts, actor, action, detail from meta.audit order by id desc limit 50"), default=str))
    return app
