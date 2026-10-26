"""Create LOCAL development users with random passphrases, written to data/dev-credentials.txt (owner-only, git-ignored).
Refuses to run against a non-local database. There are no default credentials anywhere in this project."""
import os, secrets, sys
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tessera.recon import auth, pg
from tessera.recon.settings import settings

url = settings.database_url
if not url or urlparse(url).hostname not in ("127.0.0.1", "localhost") or os.environ.get("MATCHBOOK_ENV") == "production":
    sys.exit("refusing: dev_seed only runs against a localhost database")
pg.migrate(); out = Path("data/dev-credentials.txt"); out.parent.mkdir(exist_ok=True); lines = []
existing = {u["username"] for u in auth.list_users()}
for name, role in (("ada", "admin"), ("mel", "reviewer"), ("chen", "approver"), ("vic", "viewer")):
    if name in existing: continue
    pw = secrets.token_urlsafe(18); auth.create_user(name, pw, role, actor="dev_seed"); lines.append(f"{name} ({role}): {pw}")
if lines:
    with open(out, "a") as f: f.write("\n".join(lines) + "\n")
    out.chmod(0o600)
print(f"created {len(lines)} user(s); credentials are in {out}" if lines else "dev users already exist")
pg.close()
