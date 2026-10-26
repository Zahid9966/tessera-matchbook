"""Matchbook web API: authenticated, role-checked, CSRF-protected. Postgres holds state; DuckDB holds the matching tables."""
from __future__ import annotations
import collections, hashlib, hmac, json, logging, shutil, sys, tempfile, time, uuid
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .. import config, warehouse
from ..ai import gateway
from . import ai_mapping, anomalies, audit, auth, explain, importer, layouts, pg, review, synth
from .settings import settings

WEB = Path(__file__).parent / "web"
KINDS = ("bank", "gateway", "ledger")
COOKIE = "mb_session"
SAFE = {"GET", "HEAD", "OPTIONS"}
log = logging.getLogger("matchbook"); log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler(sys.stdout); h.setFormatter(logging.Formatter("%(message)s")); log.addHandler(h)

def uploads() -> Path:
    p = config.DATA / "recon_uploads"; p.mkdir(parents=True, exist_ok=True); return p

class Login(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=256)

class NewUser(BaseModel):
    username: str
    password: str = Field(max_length=256)
    role: str
    display_name: str = ""

class Role(BaseModel):
    role: str

class Password(BaseModel):
    password: str = Field(max_length=256)

class Mappings(BaseModel):
    mappings: dict[str, dict[str, str]]

class Candidate(BaseModel):
    mapping: dict[str, str]

class Propose(BaseModel):
    kind: str
    key: str
    action: str
    note: str = Field(default="", max_length=500)

class Undo(BaseModel):
    kind: str
    key: str
    note: str = Field(default="", max_length=500)

class Link(BaseModel):
    order_id: str
    payment_id: str
    note: str = Field(default="", max_length=500)

class Note(BaseModel):
    note: str = Field(default="", max_length=500)

_logins: dict[str, collections.deque] = collections.defaultdict(collections.deque)

def _throttle(ip: str, limit: int = 20, window: int = 60) -> None:
    q = _logins[ip]; now = time.time()
    while q and q[0] < now - window: q.popleft()
    if len(q) >= limit: raise HTTPException(429, "too many login attempts from this address; wait a minute")
    q.append(now)

def _ip(request: Request) -> str: return request.client.host if request.client else "?"

def _origin_ok(request: Request) -> bool:
    o = request.headers.get("origin")
    if not o: return True
    host = request.headers.get("host", "")
    return o.split("://", 1)[-1] == host

def guard(perm: str):
    """Dependency: valid session, CSRF token on state-changing requests, and the role must grant `perm`."""
    def dep(request: Request) -> auth.User:
        s = auth.session(request.cookies.get(COOKIE))
        if not s: raise HTTPException(401, "not signed in")
        user, csrf = s
        if request.method not in SAFE:
            if not _origin_ok(request) or not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf): raise HTTPException(403, "missing or invalid CSRF token")
        if not user.can(perm): raise HTTPException(403, f"your role ({user.role}) cannot do this")
        request.state.user = user
        return user
    return dep

def _path(kind: str) -> Path:
    if kind not in KINDS: raise HTTPException(404, f"kind must be one of {KINDS}")
    p = uploads() / f"{kind}.csv"
    if not p.exists(): raise HTTPException(409, f"no {kind} file uploaded yet")
    return p

def create_app() -> FastAPI:
    if not settings.database_url: raise pg.NotConfigured("MATCHBOOK_DATABASE_URL is not set (see .env.example)")
    pg.migrate(); warehouse.connect(); warehouse.cursor().execute("create schema if not exists recon")
    if review.loaded(): review.sync_links()
    app = FastAPI(title="Matchbook", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        t0 = time.time(); rid = uuid.uuid4().hex[:12]
        host = request.headers.get("host", "").split(":")[0]
        if host not in settings.allowed_hosts: return JSONResponse({"detail": "host not allowed"}, status_code=400)
        resp: Response = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Request-Id": rid,
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        resp.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"      # static files revalidate (ETag), so upgrades are picked up
        u = getattr(request.state, "user", None)           # one structured line per request; never bodies, cookies or credentials
        log.info(json.dumps({"rid": rid, "m": request.method, "p": request.url.path, "s": resp.status_code, "ms": round((time.time() - t0) * 1000), "u": u.username if u else None}))
        return resp

    @app.exception_handler(auth.AuthError)
    async def _auth_err(_, e: auth.AuthError): return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.exception_handler(review.ReviewError)
    async def _rev_err(_, e: review.ReviewError): return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.get("/")
    def index(): return FileResponse(WEB / "index.html")

    @app.get("/app.js")
    def js(): return FileResponse(WEB / "app.js", media_type="application/javascript")

    @app.get("/app.css")
    def css(): return FileResponse(WEB / "app.css", media_type="text/css")

    @app.get("/api/health")
    def health(): return {"status": "ok", "bootstrap_needed": auth.count_users() == 0}

    @app.get("/api/recon/system")
    def system(user: auth.User = Depends(guard("view"))):
        return {"llm": gateway.health(), "loaded": review.loaded(), "uploaded": {k: (uploads() / f"{k}.csv").exists() for k in KINDS},
                "require_checker": settings.require_checker, "demo": settings.enable_demo}

    # ---- auth
    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response):
        if not _origin_ok(request): raise HTTPException(403, "cross-origin login refused")
        _throttle(_ip(request))
        token, user, csrf = auth.login(body.username, body.password, _ip(request))
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=settings.cookie_secure, max_age=int(settings.session_hours * 3600), path="/")
        return {"user": user.__dict__, "csrf": csrf, "permissions": sorted(p for p, roles in auth.PERMISSIONS.items() if user.role in roles)}

    @app.get("/api/auth/me")
    def me(request: Request):
        s = auth.session(request.cookies.get(COOKIE))
        if not s: raise HTTPException(401, "not signed in")
        user, csrf = s
        return {"user": user.__dict__, "csrf": csrf, "permissions": sorted(p for p, roles in auth.PERMISSIONS.items() if user.role in roles)}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user: auth.User = Depends(guard("view"))):
        auth.logout(request.cookies.get(COOKIE), user); response.delete_cookie(COOKIE, path="/"); return {"ok": True}

    # ---- admin
    @app.get("/api/admin/users")
    def users(user: auth.User = Depends(guard("manage_users"))): return auth.list_users()

    @app.post("/api/admin/users")
    def new_user(b: NewUser, user: auth.User = Depends(guard("manage_users"))):
        return {"id": auth.create_user(b.username, b.password, b.role, b.display_name, actor=user.username, actor_id=user.id)}

    @app.post("/api/admin/users/{uid}/role")
    def role(uid: int, b: Role, user: auth.User = Depends(guard("manage_users"))): auth.set_role(uid, b.role, user); return {"ok": True}

    @app.post("/api/admin/users/{uid}/disable")
    def disable(uid: int, user: auth.User = Depends(guard("manage_users"))): auth.set_disabled(uid, True, user); return {"ok": True}

    @app.post("/api/admin/users/{uid}/enable")
    def enable(uid: int, user: auth.User = Depends(guard("manage_users"))): auth.set_disabled(uid, False, user); return {"ok": True}

    @app.post("/api/admin/users/{uid}/password")
    def password(uid: int, b: Password, user: auth.User = Depends(guard("manage_users"))): auth.reset_password(uid, b.password, user); return {"ok": True}

    @app.get("/api/admin/audit")
    def audit_list(limit: int = 100, user: auth.User = Depends(guard("audit"))): return audit.recent(limit)

    @app.get("/api/admin/audit/verify")
    def audit_verify(user: auth.User = Depends(guard("audit"))): return audit.verify()

    # ---- import with mapping confirmation
    @app.put("/api/recon/upload/{kind}")
    async def upload(kind: str, request: Request, name: str = "", user: auth.User = Depends(guard("import"))):
        if kind not in KINDS: raise HTTPException(404, f"kind must be one of {KINDS}")
        body = await request.body()
        if len(body) > settings.max_upload_bytes: raise HTTPException(413, "file too large (50 MB max)")
        if not body.strip(): raise HTTPException(400, "empty file")
        dest = uploads() / f"{kind}.csv"; dest.write_bytes(body)           # fixed name: the client never chooses a path
        try: headers = importer.read_header(dest)
        except Exception: dest.unlink(missing_ok=True); raise HTTPException(400, "could not read this as a CSV file")
        audit.log(user.username, "import.upload", {"kind": kind, "file": name[:80], "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}, user.id, _ip(request))
        return {"kind": kind, "file": name[:80], "bytes": len(body), "columns": headers}

    @app.get("/api/recon/mapping/{kind}")
    def mapping(kind: str, llm: bool = True, user: auth.User = Depends(guard("import"))):
        p = _path(kind); r = ai_mapping.resolve(kind, p, use_llm=llm)
        return {**r, "headers": importer.read_header(p), "samples": ai_mapping.sample_rows(p, 3), "fields": ai_mapping.FIELD_HELP[kind], "required": importer.REQUIRED[kind]}

    @app.post("/api/recon/validate/{kind}")
    def validate(kind: str, c: Candidate, user: auth.User = Depends(guard("import"))): return {"problems": ai_mapping.check_mapping(kind, _path(kind), c.mapping)}

    @app.post("/api/recon/import")
    def do_import(body: Mappings, request: Request, user: auth.User = Depends(guard("import"))):
        missing = [k for k in KINDS if k not in body.mappings or not (uploads() / f"{k}.csv").exists()]
        if missing: raise HTTPException(409, f"missing files or mappings for: {missing}")
        problems = {k: ai_mapping.check_mapping(k, uploads() / f"{k}.csv", body.mappings[k]) for k in KINDS}
        if any(problems.values()): raise HTTPException(422, {"problems": {k: v for k, v in problems.items() if v}})
        res = {k: importer.load(k, uploads() / f"{k}.csv", body.mappings[k]) for k in KINDS}
        review.sync_links(); anomalies.run()                                    # links are re-applied from approved decisions
        audit.log(user.username, "import.run", {k: v["rows"] for k, v in res.items()}, user.id, _ip(request))
        return {"rows": {k: v["rows"] for k, v in res.items()}, "summary": review.summary()}

    @app.post("/api/recon/demo")
    def demo(messy: bool = True, seed: int = 7, user: auth.User = Depends(guard("manage_users"))):
        if not settings.enable_demo: raise HTTPException(403, "demo data is disabled (MATCHBOOK_ENABLE_DEMO)")
        tmp = Path(tempfile.mkdtemp()); src = tmp / "src"; synth.generate(n_orders=4000, days=240, seed=seed, out=src)
        if messy:
            for x in [x for x in layouts.render(src, tmp / "r") if x["name"] in ("bank_tran", "gw_credited", "ledger_invoice")]: shutil.copy(x["path"], uploads() / f"{x['kind']}.csv")
        else:
            for k, fn in layouts.FILE.items(): shutil.copy(src / fn, uploads() / f"{k}.csv")
        shutil.rmtree(tmp, ignore_errors=True); audit.log(user.username, "demo.load", {"messy": messy, "seed": seed}, user.id)
        return {"loaded": list(KINDS), "messy": messy}

    # ---- review (read)
    @app.get("/api/recon/summary")
    def summary(user: auth.User = Depends(guard("view"))): return review.summary()

    @app.get("/api/recon/exceptions/{kind}")
    def exc(kind: str, state: str = "pending", limit: int = 50, offset: int = 0, user: auth.User = Depends(guard("view"))):
        return review.exceptions(kind, state, max(1, min(limit, 200)), max(offset, 0))

    @app.get("/api/recon/flags")
    def flg(state: str = "pending", limit: int = 50, offset: int = 0, user: auth.User = Depends(guard("view"))):
        return review.flags(state, max(1, min(limit, 200)), max(offset, 0))

    @app.get("/api/recon/candidates/{order_id}")
    def cands(order_id: str, user: auth.User = Depends(guard("view"))): return {"order_id": order_id, "candidates": review.candidates(order_id)}

    @app.get("/api/recon/explain/{row_id}")
    def expl(row_id: int, llm: bool = False, user: auth.User = Depends(guard("view"))):
        try: return explain.explain(row_id, use_llm=llm)
        except KeyError: raise HTTPException(404, "no such flagged transaction")

    @app.get("/api/recon/proposals")
    def proposals(user: auth.User = Depends(guard("view"))): return review.open_proposals()

    # ---- review (maker/checker)
    @app.post("/api/recon/review")
    def propose(b: Propose, request: Request, user: auth.User = Depends(guard("propose"))):
        if b.action == "link": raise HTTPException(400, "use /api/recon/link for links")
        return review.propose(user, b.kind, b.key, b.action, b.note, ip=_ip(request))

    @app.post("/api/recon/undo")
    def undo(b: Undo, request: Request, user: auth.User = Depends(guard("propose"))): return review.propose(user, b.kind, b.key, "undo", b.note, ip=_ip(request))

    @app.post("/api/recon/link")
    def link(b: Link, request: Request, user: auth.User = Depends(guard("propose"))): return review.propose_link(user, b.order_id, b.payment_id, b.note, _ip(request))

    @app.post("/api/recon/proposals/{did}/approve")
    def approve(did: int, b: Note, request: Request, user: auth.User = Depends(guard("approve"))): return review.approve(user, did, b.note, _ip(request))

    @app.post("/api/recon/proposals/{did}/decline")
    def decline(did: int, b: Note, request: Request, user: auth.User = Depends(guard("approve"))): return review.decline(user, did, b.note, _ip(request))

    @app.post("/api/recon/proposals/{did}/withdraw")
    def withdraw(did: int, request: Request, user: auth.User = Depends(guard("propose"))): return review.withdraw(user, did, _ip(request))
    return app

def main() -> None:
    import uvicorn
    uvicorn.run(create_app(), host=settings.host, port=settings.port, proxy_headers=False, server_header=False)

if __name__ == "__main__":
    main()
