from __future__ import annotations
import argparse, json
from .pipeline import jobs, models

def main() -> None:
    ap = argparse.ArgumentParser("tessera")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate"); sub.add_parser("ingest"); sub.add_parser("transform")
    sub.add_parser("quality"); sub.add_parser("pipeline"); sub.add_parser("lineage")
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=8787); sv.add_argument("--schedule", type=int, default=0, help="run pipeline every N seconds")
    a = ap.parse_args()
    if a.cmd == "lineage": print(json.dumps(models.lineage(), indent=2)); return
    if a.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(a.schedule), host="127.0.0.1", port=a.port); return
    r = jobs.run(a.cmd, trigger="cli")
    print(r["status"]); print(r["detail"][:1500])
    raise SystemExit(0 if r["status"] == "success" else 1)

if __name__ == "__main__":
    main()
