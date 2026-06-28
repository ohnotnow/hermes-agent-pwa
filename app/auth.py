"""Browser session auth for the hap gateway (stdlib only).

The single shared bearer token is the master credential. The browser never
holds it: on login the user presents the token OR a 6-digit PIN derived from
it, and we set a signed, HttpOnly session cookie. Agent endpoints keep using
the raw bearer token; browser endpoints accept the session cookie (or the
bearer, for curl/scripts).

PIN = token hashed to 6 decimal digits (see ant: a leaked PIN must not leak the
token, so it's a hash, not a slice). 6 digits is brute-forceable, so the login
route is rate-limited + locked out (PRD §12).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.config import load_settings

settings = load_settings()

SESSION_COOKIE = "hap_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

# HMAC key for the signed session cookie, derived from the bearer token so we
# need no separate secret and it stays stable across restarts.
_SESSION_KEY = hashlib.sha256(b"hap-session:" + settings.auth_token.encode()).digest()


def pin_for_token(token: str) -> str:
    if not token:
        return ""
    n = int.from_bytes(hashlib.sha256(token.encode()).digest(), "big") % 1_000_000
    return f"{n:06d}"


PIN = pin_for_token(settings.auth_token)


# ── signed session cookie ─────────────────────────────────────────────────

def make_session() -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": int(time.time())}).encode()
    ).decode()
    sig = hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    payload, _, sig = cookie.rpartition(".")
    expected = hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return False
    return (time.time() - float(data.get("iat", 0))) <= SESSION_MAX_AGE


# ── secret checks ─────────────────────────────────────────────────────────

def check_secret(secret: str) -> bool:
    """True if the presented secret is the bearer token or the derived PIN."""
    if not settings.auth_token:
        return False
    secret = secret or ""
    ok_token = hmac.compare_digest(secret, settings.auth_token)
    ok_pin = bool(PIN) and hmac.compare_digest(secret, PIN)
    return ok_token or ok_pin


def _bearer_ok(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and settings.auth_token:
        return hmac.compare_digest(auth[len("Bearer "):], settings.auth_token)
    return False


def is_authed(request: Request) -> bool:
    return valid_session(request.cookies.get(SESSION_COOKIE)) or _bearer_ok(request)


# ── FastAPI dependencies ──────────────────────────────────────────────────

def require_session(request: Request) -> None:
    if not valid_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(401, "not authenticated")


def require_session_or_bearer(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(401, "not authenticated")


def require_bearer(request: Request) -> None:
    if not _bearer_ok(request):
        raise HTTPException(401, "invalid or missing bearer token")


def require_same_origin(request: Request) -> None:
    """Strict-origin check for state-changing requests. Browsers send Origin;
    non-browser clients (curl) don't, and the SameSite=Lax cookie covers them."""
    origin = request.headers.get("origin")
    if not origin:
        return
    if urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "cross-origin request rejected")


# ── login rate-limit / lockout ────────────────────────────────────────────

class LoginLimiter:
    def __init__(self, max_fails: int = 5, window: int = 300, lockout: int = 300):
        self.max_fails = max_fails
        self.window = window
        self.lockout = lockout
        self._fails: list[float] = []
        self._locked_until = 0.0

    def allowed(self) -> bool:
        return time.time() >= self._locked_until

    def record_fail(self) -> None:
        now = time.time()
        self._fails = [t for t in self._fails if now - t < self.window]
        self._fails.append(now)
        if len(self._fails) >= self.max_fails:
            self._locked_until = now + self.lockout
            self._fails = []

    def record_success(self) -> None:
        self._fails = []
        self._locked_until = 0.0


login_limiter = LoginLimiter()
