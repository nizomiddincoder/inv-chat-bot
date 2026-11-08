"""Session-based authentication for the admin panel.

The owner logs in once with ADMIN_USER / ADMIN_PASSWORD (env variables —
never stored in code) and receives a signed HttpOnly cookie. Every admin
page and admin API endpoint requires this cookie; the Telegram webhook
keeps its own separate secret and is unaffected.

Tokens are HMAC-SHA256 signed with a key derived from SESSION_SECRET when
set, otherwise from the admin credentials — so tokens stay valid across
redeploys but are useless without the same secret.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time

from .config import settings

log = logging.getLogger("auth")

SESSION_COOKIE = "markaz_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # one week


def _key() -> bytes:
    base = (
        os.getenv("SESSION_SECRET", "").strip()
        or f"{settings.ADMIN_USER}:{settings.ADMIN_PASSWORD}:{settings.PUBLIC_URL}"
    )
    return hashlib.sha256(base.encode("utf-8")).digest()


def _sign(data: bytes) -> str:
    return hmac.new(_key(), data, hashlib.sha256).hexdigest()


def create_session(login: str) -> str:
    """Create a signed session token for `login`."""
    payload = json.dumps({"login": login, "exp": int(time.time()) + SESSION_TTL_SECONDS})
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{raw}.{_sign(raw.encode('ascii'))}"


def verify_session(token: str) -> str | None:
    """Return the login if the token is valid and not expired, else None."""
    if not token or "." not in token:
        return None
    raw, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw.encode("ascii")), signature):
        return None
    padding = "=" * (-len(raw) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return data.get("login") or None


def check_credentials(login: str, password: str) -> bool:
    """Constant-time comparison against the env credentials."""
    expected_login = settings.ADMIN_USER
    expected_password = settings.ADMIN_PASSWORD
    if not expected_password:
        return False  # panel disabled without ADMIN_PASSWORD
    login_ok = hmac.compare_digest((login or "").encode(), expected_login.encode())
    password_ok = hmac.compare_digest((password or "").encode(), expected_password.encode())
    return login_ok and password_ok
