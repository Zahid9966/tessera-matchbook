"""Admin CLI. There are no default credentials: the first administrator is created here.
   python -m tessera.recon.admin create-user alice --role admin
   python -m tessera.recon.admin list-users | verify-audit | migrate"""
from __future__ import annotations
import argparse, getpass, sys
from . import audit, auth, pg

def main(argv=None) -> int:
    ap = argparse.ArgumentParser("matchbook-admin"); sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user"); c.add_argument("username"); c.add_argument("--role", required=True, choices=auth.ROLES); c.add_argument("--display-name", default="")
    c.add_argument("--password-stdin", action="store_true", help="read the password from stdin (for scripts) instead of prompting")
    sub.add_parser("list-users"); sub.add_parser("verify-audit"); sub.add_parser("migrate")
    a = ap.parse_args(argv)
    pg.migrate()
    if a.cmd == "migrate": print("schema is up to date"); return 0
    if a.cmd == "list-users":
        for u in auth.list_users(): print(f"{u['username']:20s} {u['role']:9s} {'DISABLED' if u['disabled'] else ''}")
        return 0
    if a.cmd == "verify-audit":
        r = audit.verify(); print(r); return 0 if r["ok"] else 2
    pw = sys.stdin.readline().rstrip("\n") if a.password_stdin else getpass.getpass("Password: ")
    if not a.password_stdin and getpass.getpass("Repeat: ") != pw: print("passwords differ"); return 1
    try: print("created user id", auth.create_user(a.username, pw, a.role, a.display_name, actor="cli"))
    except auth.AuthError as e: print("error:", e); return 1
    return 0

if __name__ == "__main__":
    try: code = main()
    finally: pg.close()
    raise SystemExit(code)
