"""Authentication, authorisation, maker/checker and the review flow, tested through the real HTTP API against real Postgres."""
import threading, warnings
import pytest
warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from tessera import warehouse
from tessera.recon import admin, app as appmod, audit, auth, importer, pg, review
from tessera.recon.settings import settings

PW = "correct-horse-battery"
K = "ledger_without_payment"

@pytest.fixture(scope="module")
def world():
    """One app, a fixed cast of users, and a loaded demo statement."""
    pg.reset_for_tests(); settings.enable_demo = True
    app = appmod.create_app()
    for name, role in (("ada", "admin"), ("adam", "admin"), ("mel", "reviewer"), ("chen", "approver"), ("cora", "approver"), ("vic", "viewer")):
        auth.create_user(name, PW, role)
    admin_c = login(app, "ada"); assert admin_c.post("/api/recon/demo?messy=false&seed=4").status_code == 200
    maps = {k: importer.propose_mapping(k, importer.read_header(appmod.uploads() / f"{k}.csv")) for k in appmod.KINDS}
    assert admin_c.post("/api/recon/import", json={"mappings": maps}).status_code == 200
    yield app
    settings.enable_demo = False

def login(app, username, password=PW, header=True):
    appmod._logins.clear()
    c = TestClient(app); r = c.post("/api/auth/login", json={"username": username, "password": password}); assert r.status_code == 200, r.text
    if header: c.headers.update({"X-CSRF-Token": r.json()["csrf"]})
    return c

@pytest.fixture()
def clean(world):
    with pg.tx() as c: c.execute("delete from decisions")
    review.sync_links()
    return world

def first_item(c, kind=K): return c.get(f"/api/recon/exceptions/{kind}?limit=1").json()["items"][0]["item_key"]

# ======================== authentication ========================================================================
def test_nothing_is_reachable_without_signing_in(world):
    c = TestClient(world)
    for m, p in (("get", "/api/recon/summary"), ("get", "/api/recon/flags"), ("get", "/api/recon/proposals"), ("get", "/api/admin/users"), ("get", "/api/admin/audit"),
                 ("get", "/api/recon/system"), ("post", "/api/recon/review"), ("put", "/api/recon/upload/bank"), ("get", "/api/recon/explain/1")):
        assert getattr(c, m)(p).status_code == 401, p
    h = c.get("/api/health").json(); assert set(h) == {"status", "bootstrap_needed"}          # public, and says nothing sensitive

def test_wrong_password_and_unknown_user_look_identical(world):
    c = TestClient(world); appmod._logins.clear()
    a = c.post("/api/auth/login", json={"username": "mel", "password": "wrong-password-123"}); b = c.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-123"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()

def test_account_locks_after_repeated_failures_even_for_the_right_password(world):
    c = TestClient(world)
    for _ in range(settings.max_failed_logins): appmod._logins.clear(); c.post("/api/auth/login", json={"username": "vic", "password": "nope-nope-nope-1"})
    appmod._logins.clear(); r = c.post("/api/auth/login", json={"username": "vic", "password": PW}); assert r.status_code == 429
    pg.q("select 1"); 
    with pg.tx() as cn: cn.execute("update users set locked_until = now() - interval '1 minute', failed_logins = 0 where username='vic'")
    appmod._logins.clear(); assert c.post("/api/auth/login", json={"username": "vic", "password": PW}).status_code == 200

def test_password_policy_and_input_limits(world):
    for user, bad in (("newbie", "short1"), ("newbie", "aaaaaaaaaaaaaaaa"), ("victor", "my-victor-passphrase")):
        with pytest.raises(auth.AuthError): auth.create_user(user, bad, "viewer")
    for name in ("Bad Name", "a", "x" * 50, "semi;colon", "ro'ot"):
        with pytest.raises(auth.AuthError): auth.create_user(name, PW, "viewer")
    assert TestClient(world).post("/api/auth/login", json={"username": "x", "password": "p" * 5000}).status_code == 422

def test_session_cookie_is_httponly_samesite_strict(world):
    c = TestClient(world); appmod._logins.clear(); r = c.post("/api/auth/login", json={"username": "mel", "password": PW}); ck = r.headers["set-cookie"].lower()
    assert "httponly" in ck and "samesite=strict" in ck and "path=/" in ck

def test_csrf_token_is_required_and_cross_origin_is_refused(world):
    c = login(world, "mel", header=False)
    assert c.post("/api/recon/undo", json={"kind": K, "key": "x"}).status_code == 403                                   # no token
    assert c.post("/api/recon/undo", json={"kind": K, "key": "x"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    ok = login(world, "mel"); assert ok.post("/api/recon/undo", json={"kind": K, "key": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert ok.post("/api/recon/undo", json={"kind": K, "key": "x"}).status_code in (400, 409)                            # token accepted; request itself is invalid

def test_logout_expiry_and_idle_timeout_end_the_session(world):
    c = login(world, "mel"); assert c.get("/api/recon/summary").status_code == 200
    c.post("/api/auth/logout"); assert c.get("/api/recon/summary").status_code == 401
    c = login(world, "mel")
    with pg.tx() as cn: cn.execute("update sessions set expires_at = now() - interval '1 second' where user_id=(select id from users where username='mel')")
    assert c.get("/api/recon/summary").status_code == 401
    c = login(world, "mel")
    with pg.tx() as cn: cn.execute("update sessions set last_seen_at = now() - interval '2 hours' where user_id=(select id from users where username='mel')")
    assert c.get("/api/recon/summary").status_code == 401

def test_disabling_or_changing_a_user_signs_them_out_and_last_admin_is_protected(world):
    admin_c = login(world, "ada"); uid = {u["username"]: u["id"] for u in admin_c.get("/api/admin/users").json()}
    v = login(world, "vic"); assert admin_c.post(f"/api/admin/users/{uid['vic']}/disable").status_code == 200
    assert v.get("/api/recon/summary").status_code == 401
    appmod._logins.clear(); assert TestClient(world).post("/api/auth/login", json={"username": "vic", "password": PW}).status_code == 401
    admin_c.post(f"/api/admin/users/{uid['vic']}/enable")
    m = login(world, "mel"); admin_c.post(f"/api/admin/users/{uid['mel']}/role", json={"role": "reviewer"}); assert m.get("/api/recon/summary").status_code == 401   # role change revokes sessions
    admin_c = login(world, "ada")
    assert admin_c.post(f"/api/admin/users/{uid['adam']}/role", json={"role": "viewer"}).status_code == 200      # a second admin exists, so fine
    r = admin_c.post(f"/api/admin/users/{uid['ada']}/role", json={"role": "viewer"}); assert r.status_code == 409 and "last administrator" in r.text
    admin_c.post(f"/api/admin/users/{uid['adam']}/role", json={"role": "admin"})

def test_password_reset_signs_the_user_out_everywhere(world):
    a = login(world, "ada"); uid = {u["username"]: u["id"] for u in a.get("/api/admin/users").json()}["vic"]
    v = login(world, "vic"); assert a.post(f"/api/admin/users/{uid}/password", json={"password": "a-brand-new-passphrase"}).status_code == 200
    assert v.get("/api/recon/summary").status_code == 401
    a.post(f"/api/admin/users/{uid}/password", json={"password": PW})

def test_unknown_host_is_refused_and_hardening_headers_are_set(world):
    c = TestClient(world); assert c.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    r = c.get("/"); h = r.headers
    assert "script-src 'self'" in h["content-security-policy"] and h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert c.get("/api/health").headers["cache-control"] == "no-store" and c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    assert "<script>" not in r.text.replace("<script src", "")                                      # no inline script, so the CSP can stay strict

def test_login_attempts_are_throttled_per_address(world):
    c = TestClient(world); appmod._logins.clear(); codes = [c.post("/api/auth/login", json={"username": f"ghost{i}", "password": "wrong-password-1"}).status_code for i in range(25)]
    assert 429 in codes and codes[0] == 401; appmod._logins.clear()

# ======================== roles ===========================================================================
def test_roles_limit_what_each_person_can_do(clean):
    v, m, ch, ad = (login(clean, n) for n in ("vic", "mel", "chen", "ada"))
    key = first_item(v)
    assert v.get("/api/recon/summary").status_code == 200
    assert v.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"}).status_code == 403          # viewers read only
    assert v.put("/api/recon/upload/bank", content=b"a,b\n1,2\n").status_code == 403
    assert m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"}).status_code == 200            # makers propose
    pid = m.get("/api/recon/proposals").json()[0]["id"]
    assert m.post(f"/api/recon/proposals/{pid}/approve", json={}).status_code == 403                                      # but cannot approve
    assert ch.put("/api/recon/upload/bank", content=b"a,b\n1,2\n").status_code == 403                                     # approvers do not import
    for c in (v, m, ch): assert c.get("/api/admin/users").status_code == 403
    assert v.get("/api/admin/audit").status_code == 403 and m.get("/api/admin/audit").status_code == 403 and ch.get("/api/admin/audit").status_code == 200
    assert ch.post("/api/admin/users", json={"username": "evil", "password": PW, "role": "admin"}).status_code == 403   # no privilege escalation
    assert ad.post("/api/recon/demo?messy=false&seed=4").status_code == 200 and m.post("/api/recon/demo").status_code == 403

def test_demo_is_off_unless_enabled(world, monkeypatch):
    monkeypatch.setattr(settings, "enable_demo", False); assert login(world, "ada").post("/api/recon/demo").status_code == 403

# ======================== maker / checker =================================================================
def test_a_proposal_changes_nothing_until_a_different_person_approves(clean):
    m, ch = login(clean, "mel"), login(clean, "chen"); key = first_item(m); before = m.get("/api/recon/summary").json()["exceptions"]
    assert m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm", "note": "chase"}).json()["status"] == "proposed"
    assert m.get(f"/api/recon/exceptions/{K}?state=confirmed").json()["total"] == 0                                        # not effective yet
    it = m.get(f"/api/recon/exceptions/{K}?limit=1").json()["items"][0]; assert it["item_key"] == key and it["proposal"]["by"] == "mel"
    assert m.get("/api/recon/summary").json()["exceptions"] == [{**e, "awaiting": e["awaiting"] + (1 if e["kind"] == K else 0)} for e in before]
    pid = ch.get("/api/recon/proposals").json()[0]["id"]; assert ch.post(f"/api/recon/proposals/{pid}/approve", json={"note": "agreed"}).status_code == 200
    got = m.get(f"/api/recon/exceptions/{K}?state=confirmed").json()["items"][0]; assert got["item_key"] == key and got["note"] == "chase"

def test_nobody_can_approve_their_own_proposal_not_even_an_admin(clean):
    ad = login(clean, "ada"); key = first_item(ad)
    assert ad.post("/api/recon/review", json={"kind": K, "key": key, "action": "reject"}).status_code == 200
    pid = ad.get("/api/recon/proposals").json()[0]["id"]
    r = ad.post(f"/api/recon/proposals/{pid}/approve", json={}); assert r.status_code == 403 and "four-eyes" in r.text
    assert ad.post(f"/api/recon/proposals/{pid}/decline", json={}).status_code == 403                                      # own proposals are withdrawn, not declined
    assert ad.post(f"/api/recon/proposals/{pid}/withdraw").status_code == 200

def test_the_database_itself_refuses_self_approval(clean):
    """Defence in depth: bypass the application entirely and try it in SQL."""
    m = login(clean, "mel"); key = first_item(m); m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"})
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg.tx() as c: c.execute("update decisions set status='approved', decided_by=proposed_by, decided_at=now() where item_key=%s", (key,))

def test_decline_withdraw_and_conflicts(clean):
    m, ch, co = login(clean, "mel"), login(clean, "chen"), login(clean, "cora"); key = first_item(m)
    m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"}); pid = m.get("/api/recon/proposals").json()[0]["id"]
    r = co.post("/api/recon/review", json={"kind": K, "key": key, "action": "reject"}); assert r.status_code == 409 and "open proposal" in r.text      # one proposal per item
    assert ch.post(f"/api/recon/proposals/{pid}/withdraw").status_code == 403                                               # only the proposer may withdraw
    assert ch.post(f"/api/recon/proposals/{pid}/decline", json={"note": "not convinced"}).status_code == 200
    assert m.get(f"/api/recon/exceptions/{K}?state=confirmed").json()["total"] == 0 and key in [i["item_key"] for i in m.get(f"/api/recon/exceptions/{K}").json()["items"]]
    assert ch.post(f"/api/recon/proposals/{pid}/approve", json={}).status_code == 409                                       # already decided
    assert ch.post("/api/recon/proposals/999999/approve", json={}).status_code == 404

def test_undo_is_also_a_proposal_and_pops_one_step(clean):
    m, ch = login(clean, "mel"), login(clean, "chen"); key = first_item(m)
    def decide(c, action):
        assert m.post("/api/recon/review" if action != "undo" else "/api/recon/undo", json={"kind": K, "key": key, "action": action} if action != "undo" else {"kind": K, "key": key}).status_code == 200
        assert ch.post(f"/api/recon/proposals/{ch.get('/api/recon/proposals').json()[0]['id']}/approve", json={}).status_code == 200
    decide(m, "confirm"); decide(m, "reject"); decide(m, "undo")
    assert m.get(f"/api/recon/exceptions/{K}?state=confirmed").json()["items"][0]["item_key"] == key                          # back to confirm
    decide(m, "undo"); assert m.get(f"/api/recon/exceptions/{K}?state=confirmed").json()["total"] == 0
    assert m.post("/api/recon/undo", json={"kind": K, "key": key}).status_code == 409                                       # nothing left to undo
    assert m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"}).status_code == 200; ch.post(f"/api/recon/proposals/{ch.get('/api/recon/proposals').json()[0]['id']}/approve", json={})
    assert m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"}).status_code == 409                # no-op rejected

def test_manual_link_needs_approval_then_changes_matching_and_undo_reverses_it(clean):
    m, ch = login(clean, "mel"), login(clean, "chen"); g = lambda p: m.get(f"/api/recon/{p}").json()
    before = {e["kind"]: e["n"] for e in g("summary")["exceptions"]}; rate0 = g("summary")["l1_match_rate"]; pair = None
    for it in g(f"exceptions/{K}?limit=200")["items"]:
        c = g(f"candidates/{it['item_key']}")["candidates"]
        if len(c) == 1: pair = (it["item_key"], c[0]["payment_id"]); break
    assert pair
    assert m.post("/api/recon/link", json={"order_id": pair[0], "payment_id": pair[1]}).status_code == 200
    assert {e["kind"]: e["n"] for e in g("summary")["exceptions"]} == before and g("summary")["l1_match_rate"] == rate0       # proposal alone changes nothing
    assert login(clean, "cora").post("/api/recon/link", json={"order_id": "ORD0000001", "payment_id": pair[1]}).status_code == 409   # payment already in a proposal
    ch.post(f"/api/recon/proposals/{ch.get('/api/recon/proposals').json()[0]['id']}/approve", json={})
    after = {e["kind"]: e["n"] for e in g("summary")["exceptions"]}
    assert after[K] == before[K] - 1 and after["gateway_without_order"] == before["gateway_without_order"] - 1 and g("summary")["l1_match_rate"] > rate0
    assert g(f"exceptions/{K}?state=linked")["items"][0]["item_key"] == pair[0]
    assert m.post("/api/recon/link", json={"order_id": pair[0], "payment_id": pair[1]}).status_code == 409                   # cannot link twice
    assert m.post("/api/recon/undo", json={"kind": K, "key": pair[0]}).status_code == 200
    ch.post(f"/api/recon/proposals/{ch.get('/api/recon/proposals').json()[0]['id']}/approve", json={})
    assert {e["kind"]: e["n"] for e in g("summary")["exceptions"]} == before and g("summary")["l1_match_rate"] == rate0

def test_a_link_proposal_that_went_stale_cannot_be_approved(clean):
    m, ch = login(clean, "mel"), login(clean, "chen"); g = lambda p: m.get(f"/api/recon/{p}").json(); pair = None
    for it in g(f"exceptions/{K}?limit=200")["items"]:
        c = g(f"candidates/{it['item_key']}")["candidates"]
        if c: pair = (it["item_key"], c[0]["payment_id"]); break
    m.post("/api/recon/link", json={"order_id": pair[0], "payment_id": pair[1]}); pid = ch.get("/api/recon/proposals").json()[0]["id"]
    warehouse.cursor().execute("delete from recon.exceptions where kind='gateway_without_order' and item_key=?", [pair[1]])   # simulate the data changing underneath
    r = ch.post(f"/api/recon/proposals/{pid}/approve", json={}); assert r.status_code == 409 and "no longer" in r.text
    assert ch.post(f"/api/recon/proposals/{pid}/decline", json={}).status_code == 200
    review.sync_links()

def test_when_four_eyes_is_switched_off_decisions_apply_at_once_and_are_audited_as_such(clean, monkeypatch):
    monkeypatch.setattr(settings, "require_checker", False); m = login(clean, "mel"); key = first_item(m)
    assert m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"}).json()["auto_approved"] is True
    assert m.get(f"/api/recon/exceptions/{K}?state=confirmed").json()["items"][0]["item_key"] == key
    assert any(a["action"] == "decision.auto_approved" for a in audit.recent(20))

def test_decisions_survive_a_reimport_because_keys_are_stable(clean):
    m, ch = login(clean, "mel"), login(clean, "chen"); key = first_item(m)
    m.post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm", "note": "keep me"}); ch.post(f"/api/recon/proposals/{ch.get('/api/recon/proposals').json()[0]['id']}/approve", json={})
    fk = m.get("/api/recon/flags").json()["items"][0]["item_key"]; m.post("/api/recon/review", json={"kind": "flag", "key": fk, "action": "reject"}); ch.post(f"/api/recon/proposals/{ch.get('/api/recon/proposals').json()[0]['id']}/approve", json={})
    maps = {k: importer.propose_mapping(k, importer.read_header(appmod.uploads() / f"{k}.csv")) for k in appmod.KINDS}
    assert m.post("/api/recon/import", json={"mappings": maps}).status_code == 200
    assert m.get(f"/api/recon/exceptions/{K}?state=confirmed").json()["items"][0]["note"] == "keep me"
    assert fk in [i["item_key"] for i in m.get("/api/recon/flags?state=rejected").json()["items"]]

def test_two_approvers_racing_on_one_proposal_yield_exactly_one_approval(clean):
    m = login(clean, "mel"); m.post("/api/recon/review", json={"kind": K, "key": first_item(m), "action": "confirm"})
    pid = m.get("/api/recon/proposals").json()[0]["id"]; out = []
    def go(n): out.append(login(clean, n).post(f"/api/recon/proposals/{pid}/approve", json={}).status_code)
    ts = [threading.Thread(target=go, args=(n,)) for n in ("chen", "cora", "ada", "adam")]; [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(out) == [200, 409, 409, 409]

def test_two_makers_racing_to_propose_on_one_item_yield_exactly_one_proposal(clean):
    key = first_item(login(clean, "mel")); out = []
    def go(n): out.append(login(clean, n).post("/api/recon/review", json={"kind": K, "key": key, "action": "confirm"}).status_code)
    ts = [threading.Thread(target=go, args=(n,)) for n in ("mel", "chen", "cora", "ada")]; [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(out) == [200, 409, 409, 409]

# ======================== input handling ====================================================================
@pytest.mark.parametrize("key", ["'; drop table decisions; --", "../../etc/passwd", "x' or '1'='1", "%00", "A" * 5000], ids=["sqli", "traversal", "or-1-1", "nul", "long"])
def test_hostile_item_keys_are_treated_as_data(clean, key):
    m = login(clean, "mel")
    for body in ({"kind": K, "key": key, "action": "confirm"}, {"kind": "flag", "key": key, "action": "confirm"}):
        assert m.post("/api/recon/review", json=body).status_code in (404, 422), body
    assert m.get(f"/api/recon/candidates/{key[:200]}").status_code in (200, 404) and pg.q("select count(*) as n from decisions")[0]["n"] >= 0

def test_unknown_kinds_actions_and_paths_are_rejected_cleanly(clean):
    m = login(clean, "mel")
    assert m.post("/api/recon/review", json={"kind": "nonsense", "key": "x", "action": "confirm"}).status_code == 400
    assert m.post("/api/recon/review", json={"kind": K, "key": "x", "action": "delete"}).status_code == 400
    assert m.post("/api/recon/review", json={"kind": K, "key": "x", "action": "link"}).status_code == 400
    assert m.get("/api/recon/exceptions/nonsense").status_code == 404 and m.put("/api/recon/upload/passwd", content=b"a\n1\n").status_code == 404
    assert len(m.get("/api/recon/flags?limit=100000&offset=-5").json()["items"]) <= 200

def test_upload_never_uses_the_client_filename_and_enforces_limits(clean, monkeypatch):
    from pathlib import Path
    m = login(clean, "mel")
    assert m.put("/api/recon/upload/bank", content=b"   ").status_code == 400
    monkeypatch.setattr(settings, "max_upload_bytes", 10); assert m.put("/api/recon/upload/bank", content=b"a,b\n" + b"1,2\n" * 20).status_code == 413
    monkeypatch.undo()
    r = m.put("/api/recon/upload/ledger?name=../../../../tmp/pwned.csv", content=open(appmod.uploads() / "ledger.csv", "rb").read())
    assert r.status_code == 200 and not Path("/tmp/pwned.csv").exists() and (appmod.uploads() / "ledger.csv").exists()

def test_a_bad_mapping_is_refused_and_nothing_is_imported(clean):
    m = login(clean, "mel"); maps = {k: importer.propose_mapping(k, importer.read_header(appmod.uploads() / f"{k}.csv")) for k in appmod.KINDS}
    maps["bank"]["debit"], maps["bank"]["credit"] = maps["bank"]["credit"], maps["bank"]["debit"]
    n = warehouse.query("select count(*) as n from recon.bank")[0]["n"]
    r = m.post("/api/recon/import", json={"mappings": maps}); assert r.status_code == 422 and "swapped" in r.text
    assert warehouse.query("select count(*) as n from recon.bank")[0]["n"] == n
    assert m.post("/api/recon/import", json={"mappings": {"bank": maps["bank"]}}).status_code == 409

# ======================== audit ============================================================================
def test_the_audit_trail_records_who_did_what_and_the_chain_verifies(clean):
    m, ch = login(clean, "mel"), login(clean, "chen"); m.post("/api/recon/review", json={"kind": K, "key": first_item(m), "action": "confirm"}); ch.post(f"/api/recon/proposals/{ch.get('/api/recon/proposals').json()[0]['id']}/approve", json={})
    acts = {(a["actor"], a["action"]) for a in audit.recent(200)}
    assert ("mel", "decision.proposed") in acts and ("chen", "decision.approved") in acts and ("mel", "auth.login") in acts and ("ada", "import.run") in acts
    assert ch.get("/api/admin/audit/verify").json()["ok"] is True

def test_audit_rows_cannot_be_edited_and_tampering_is_detected(clean):
    import psycopg
    audit.log("test", "x.y", {"n": 1})
    for sql in ("update audit_log set actor='mallory'", "delete from audit_log", "truncate audit_log"):
        with pytest.raises(psycopg.errors.RaiseException):
            with pg.tx() as c: c.execute(sql)
    assert audit.verify()["ok"] is True
    with pg.tx() as c:                                                            # an attacker with owner rights disables the trigger and rewrites history
        c.execute("alter table audit_log disable trigger audit_no_change")
        c.execute("update audit_log set detail = '{\"n\": 999}'::jsonb where id = (select max(id) from audit_log)")
        c.execute("alter table audit_log enable trigger audit_no_change")
    v = audit.verify(); assert v["ok"] is False and v["first_bad_id"] is not None
    with pg.tx() as c:
        c.execute("alter table audit_log disable trigger audit_no_change"); c.execute("update audit_log set detail = '{\"n\": 1}'::jsonb where id = %s", (v["first_bad_id"],)); c.execute("alter table audit_log enable trigger audit_no_change")
    assert audit.verify()["ok"] is True

def test_secrets_are_not_written_to_the_audit_log_or_stored(world):
    blob = str(audit.recent(500)); assert PW not in blob
    with pg.tx() as c: assert PW not in str(c.execute("select password_hash, token_hash from users u left join sessions s on s.user_id = u.id").fetchall())
    h = pg.q("select password_hash from users limit 1")[0]["password_hash"]; assert h.startswith("scrypt$") and len(h) > 60

# ======================== admin CLI + migrations ===========================================================
def test_admin_cli_creates_users_without_default_credentials(world, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("a-long-enough-passphrase\n"))
    assert admin.main(["create-user", "clitest", "--role", "reviewer", "--password-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n")); assert admin.main(["create-user", "clitest2", "--role", "viewer", "--password-stdin"]) == 1
    assert admin.main(["verify-audit"]) in (0, 2) and admin.main(["list-users"]) == 0 and "clitest" in capsys.readouterr().out

def test_migrations_are_idempotent_and_the_app_refuses_to_start_unconfigured(world, monkeypatch):
    assert pg.migrate() == []
    monkeypatch.setattr(settings, "database_url", "")
    with pytest.raises(pg.NotConfigured): appmod.create_app()
