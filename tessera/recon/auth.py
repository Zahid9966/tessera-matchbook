"""Users, passwords, sessions, roles. Standard library crypto only (scrypt, secrets, hmac)."""
from __future__ import annotations
import base64, hashlib, hmac, re, secrets
from dataclasses import dataclass
from . import audit, pg
from .settings import settings

ROLES = ("viewer", "reviewer", "approver", "admin")
PERMISSIONS = {
    "view": {"viewer", "reviewer", "approver", "admin"},
    "propose": {"reviewer", "approver", "admin"},        # makers
    "approve": {"approver", "admin"},                    # checkers
    "import": {"reviewer", "admin"},
    "manage_users": {"admin"},
    "audit": {"approver", "admin"},
}
USERNAME = re.compile(r"^[a-z0-9._-]{3,40}$")
_DUMMY = None

class AuthError(Exception):
    def __init__(self, message: str, status: int = 401): super().__init__(message); self.status = status

@dataclass
class User:
    id: int; username: str; display_name: str; role: str

    def can(self, perm: str) -> bool: return self.role in PERMISSIONS[perm]

# ---- passwords -----------------------------------------------------------------------------------------------
def _scrypt(password: str, salt: bytes, n_exp: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** n_exp, r=8, p=1, maxmem=2 ** 27, dklen=32)

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16); n = settings.scrypt_n_exp
    return f"scrypt${n}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(_scrypt(password, salt, n)).decode()

def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, salt, digest = stored.split("$"); return hmac.compare_digest(_scrypt(password, base64.b64decode(salt), int(n)), base64.b64decode(digest))
    except Exception: return False

def check_policy(password: str, username: str = "") -> None:
    if len(password) < settings.min_password_length: raise AuthError(f"password must be at least {settings.min_password_length} characters", 400)
    if username and username.lower() in password.lower(): raise AuthError("password must not contain the username", 400)
    if len(set(password)) < 5: raise AuthError("password is too repetitive", 400)

# ---- users -----------------------------------------------------------------------------------------------------
def create_user(username: str, password: str, role: str, display_name: str = "", actor: str = "cli", actor_id: int | None = None) -> int:
    username = username.strip().lower()
    if not USERNAME.match(username): raise AuthError("username: 3-40 characters of a-z 0-9 . _ -", 400)
    if role not in ROLES: raise AuthError(f"role must be one of {ROLES}", 400)
    check_policy(password, username)
    with pg.tx() as c:
        if c.execute("select 1 from users where username=%s", (username,)).fetchone(): raise AuthError("username already exists", 409)
        uid = c.execute("insert into users(username, display_name, role, password_hash) values (%s,%s,%s,%s) returning id", (username, display_name[:80], role, hash_password(password))).fetchone()["id"]
        audit.log(actor, "user.create", {"user": username, "role": role}, actor_id, conn=c)
    return uid

def list_users() -> list[dict]:
    return pg.q("select id, username, display_name, role, disabled, to_char(last_login_at, 'YYYY-MM-DD HH24:MI') as last_login from users order by username")

def count_users() -> int: return pg.q("select count(*) as n from users")[0]["n"]

def set_role(user_id: int, role: str, actor: User) -> None:
    if role not in ROLES: raise AuthError("unknown role", 400)
    with pg.tx() as c:
        row = c.execute("select username, role from users where id=%s for update", (user_id,)).fetchone()
        if not row: raise AuthError("no such user", 404)
        if row["role"] == "admin" and role != "admin" and c.execute("select count(*) as n from users where role='admin' and not disabled").fetchone()["n"] <= 1:
            raise AuthError("cannot demote the last administrator", 409)
        c.execute("update users set role=%s where id=%s", (role, user_id)); c.execute("delete from sessions where user_id=%s", (user_id,))
        audit.log(actor.username, "user.role", {"user": row["username"], "from": row["role"], "to": role}, actor.id, conn=c)

def set_disabled(user_id: int, disabled: bool, actor: User) -> None:
    with pg.tx() as c:
        row = c.execute("select username, role from users where id=%s for update", (user_id,)).fetchone()
        if not row: raise AuthError("no such user", 404)
        if disabled and row["role"] == "admin" and c.execute("select count(*) as n from users where role='admin' and not disabled").fetchone()["n"] <= 1:
            raise AuthError("cannot disable the last administrator", 409)
        c.execute("update users set disabled=%s where id=%s", (disabled, user_id))
        if disabled: c.execute("delete from sessions where user_id=%s", (user_id,))
        audit.log(actor.username, "user.disable" if disabled else "user.enable", {"user": row["username"]}, actor.id, conn=c)

def reset_password(user_id: int, new_password: str, actor: User) -> None:
    with pg.tx() as c:
        row = c.execute("select username from users where id=%s for update", (user_id,)).fetchone()
        if not row: raise AuthError("no such user", 404)
        check_policy(new_password, row["username"])
        c.execute("update users set password_hash=%s, failed_logins=0, locked_until=null where id=%s", (hash_password(new_password), user_id))
        c.execute("delete from sessions where user_id=%s", (user_id,))             # a reset signs the user out everywhere
        audit.log(actor.username, "user.password_reset", {"user": row["username"]}, actor.id, conn=c)

# ---- login / sessions ------------------------------------------------------------------------------------------
def _th(token: str) -> str: return hashlib.sha256(token.encode()).hexdigest()

def login(username: str, password: str, ip: str | None = None) -> tuple[str, User, str]:
    """Returns (cookie_token, user, csrf_token). Same error and similar timing for unknown user, wrong password, disabled or locked."""
    global _DUMMY
    if _DUMMY is None: _DUMMY = hash_password("not-a-real-password-for-timing")
    username = (username or "").strip().lower()
    with pg.tx() as c:
        u = c.execute("select * from users where username=%s for update", (username,)).fetchone()
        locked = bool(u and u["locked_until"] and c.execute("select %s > now() as l", (u["locked_until"],)).fetchone()["l"])
        ok = verify_password(password or "", u["password_hash"] if u else _DUMMY)         # always spend the hashing time
        if not u or locked or u["disabled"] or not ok:
            if u and not locked and not u["disabled"]:
                n = u["failed_logins"] + 1
                c.execute("update users set failed_logins=%s, locked_until = case when %s >= %s then now() + make_interval(secs => %s) else locked_until end where id=%s",
                          (n, n, settings.max_failed_logins, settings.lockout_minutes * 60.0, u["id"]))
                if n >= settings.max_failed_logins: audit.log("system", "auth.lockout", {"user": username}, u["id"], ip, conn=c)
            audit.log(username or "?", "auth.login_failed", {"reason": "locked" if locked else "rejected"}, u["id"] if u else None, ip, conn=c)
            c.commit()                                                                   # keep the counter even though we raise
            raise AuthError("invalid username or password" if not locked else "account temporarily locked; try again later", 401 if not locked else 429)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        c.execute("update users set failed_logins=0, locked_until=null, last_login_at=now() where id=%s", (u["id"],))
        c.execute("insert into sessions(token_hash, user_id, csrf_token, expires_at, ip) values (%s,%s,%s, now() + make_interval(secs => %s), %s)", (_th(token), u["id"], csrf, settings.session_hours * 3600, ip))
        audit.log(username, "auth.login", {}, u["id"], ip, conn=c)
        return token, User(u["id"], u["username"], u["display_name"], u["role"]), csrf

def session(token: str | None) -> tuple[User, str] | None:
    """Validate a cookie token. Enforces absolute expiry and idle timeout; refreshes last_seen."""
    if not token: return None
    with pg.tx() as c:
        r = c.execute("""select u.id, u.username, u.display_name, u.role, u.disabled, s.csrf_token, s.id as sid from sessions s join users u on u.id = s.user_id
                         where s.token_hash=%s and s.expires_at > now() and s.last_seen_at > now() - make_interval(secs => %s)""", (_th(token), settings.idle_minutes * 60.0)).fetchone()
        if not r or r["disabled"]: return None
        c.execute("update sessions set last_seen_at=now() where id=%s", (r["sid"],))
        return User(r["id"], r["username"], r["display_name"], r["role"]), r["csrf_token"]

def logout(token: str | None, user: User | None = None) -> None:
    if not token: return
    with pg.tx() as c:
        c.execute("delete from sessions where token_hash=%s", (_th(token),))
        if user: audit.log(user.username, "auth.logout", {}, user.id, conn=c)
