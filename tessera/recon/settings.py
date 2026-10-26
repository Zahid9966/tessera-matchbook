"""Runtime configuration from the environment (12-factor). Nothing secret has a default."""
from __future__ import annotations
import os
from dataclasses import dataclass, field

def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("MATCHBOOK_DATABASE_URL", ""))
    require_checker: bool = field(default_factory=lambda: _bool("MATCHBOOK_REQUIRE_CHECKER", True))   # four-eyes on every decision
    session_hours: float = field(default_factory=lambda: float(os.environ.get("MATCHBOOK_SESSION_HOURS", "8")))
    idle_minutes: float = field(default_factory=lambda: float(os.environ.get("MATCHBOOK_IDLE_MINUTES", "60")))
    cookie_secure: bool = field(default_factory=lambda: _bool("MATCHBOOK_COOKIE_SECURE", False))      # set true behind HTTPS
    allowed_hosts: tuple = field(default_factory=lambda: tuple(h.strip() for h in os.environ.get("MATCHBOOK_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if h.strip()))
    host: str = field(default_factory=lambda: os.environ.get("MATCHBOOK_HOST", "127.0.0.1"))        # 0.0.0.0 only inside a container
    port: int = field(default_factory=lambda: int(os.environ.get("MATCHBOOK_PORT", "8788")))
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    min_password_length: int = 12
    scrypt_n_exp: int = field(default_factory=lambda: int(os.environ.get("MATCHBOOK_SCRYPT_N_EXP", "15")))   # 2**15; lowered only in tests
    enable_demo: bool = field(default_factory=lambda: _bool("MATCHBOOK_ENABLE_DEMO", False))   # demo data replaces loaded data: off unless asked for
    max_upload_bytes: int = 50 * 1024 * 1024

settings = Settings()
