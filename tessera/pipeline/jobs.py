"""Job runner: a task registry, recorded runs, concurrency guard and a tiny interval scheduler."""
from __future__ import annotations
import json, threading, time, traceback
from typing import Callable
from .. import warehouse, generate
from . import ingest, models, quality

TASKS: dict[str, Callable[..., object]] = {}
_running: set[str] = set()
_guard = threading.Lock()

def task(name: str):
    def deco(fn): TASKS[name] = fn; return fn
    return deco

@task("generate")
def _generate(**kw): return generate.generate(**{k: v for k, v in kw.items() if k in ("customers", "days", "seed")})

@task("ingest")
def _ingest(force: bool = False, **_): return ingest.run(force=force)

@task("transform")
def _transform(select: list[str] | None = None, **_): return models.run(select)

@task("quality")
def _quality(run_id: int | None = None, **_):
    res = quality.run(run_id)
    bad = [r for r in res if r["status"] == "fail" or r["status"].startswith("error")]
    if bad: raise QualityFailure(res)
    return res

@task("pipeline")
def _pipeline(**kw):
    out = {"ingest": TASKS["ingest"](**kw), "transform": TASKS["transform"](**kw)}
    out["quality"] = quality.run(kw.get("run_id"))
    return out

class QualityFailure(Exception):
    def __init__(self, results): self.results = results; super().__init__(f"{sum(r['status']=='fail' for r in results)} quality tests failed")

def run(name: str, trigger: str = "manual", **kw) -> dict:
    if name not in TASKS: raise KeyError(f"unknown task {name}")
    with _guard:
        if name in _running: return {"status": "skipped", "detail": "already running"}
        _running.add(name)
    run_id = warehouse.nextid(); cur = warehouse.cursor()
    cur.execute("insert into meta.runs values (?,?,?,now(),null,'',?)", [run_id, name, "running", trigger])
    try:
        res = TASKS[name](run_id=run_id, **kw); status, detail = "success", json.dumps(res, default=str)[:4000]
    except QualityFailure as e:
        status, detail = "quality_failed", json.dumps(e.results, default=str)[:4000]
    except Exception as e:
        status, detail = "failed", f"{e}\n{traceback.format_exc()}"[:4000]
    finally:
        with _guard: _running.discard(name)
    cur.execute("update meta.runs set status=?, finished_at=now(), detail=? where run_id=?", [status, detail, run_id])
    return {"run_id": run_id, "task": name, "status": status, "detail": detail}

def history(limit: int = 30) -> list[dict]:
    return warehouse.query("select run_id, task, status, started_at, finished_at, trigger, substr(detail,1,300) as detail from meta.runs order by run_id desc limit ?", [limit])

class Scheduler(threading.Thread):
    """Runs `task` every `every` seconds. Intentionally minimal; swap for cron/Dagster later."""
    def __init__(self, schedule: dict[str, int]):
        super().__init__(daemon=True); self.schedule = schedule; self.stop = threading.Event()
    def run(self):
        last = {n: time.time() for n in self.schedule}
        while not self.stop.wait(5):
            for n, every in self.schedule.items():
                if time.time() - last[n] >= every:
                    last[n] = time.time(); run(n, trigger="schedule")
