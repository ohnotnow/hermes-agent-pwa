"""Unit tests for app.auth: PIN derivation, signed session cookies, secret
checks, the FastAPI auth guards, the same-origin check and the login limiter.

Pure functions only — no app, no HTTP. The auth token is fixed by conftest.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth

TOKEN = auth.settings.auth_token
PIN = auth.PIN


def fake_request(headers=None, cookies=None):
    """A stand-in for starlette's Request: auth only touches .headers/.cookies,
    both of which behave like dicts for our purposes."""
    return SimpleNamespace(headers=headers or {}, cookies=cookies or {})


# ── PIN derivation ──────────────────────────────────────────────────────────

def test_pin_is_six_digits_and_deterministic():
    assert PIN == auth.pin_for_token(TOKEN)
    assert len(PIN) == 6 and PIN.isdigit()


def test_pin_empty_token_is_empty():
    assert auth.pin_for_token("") == ""


def test_pin_is_a_hash_not_a_token_slice():
    # The docstring's whole point: a leaked PIN must not reveal the token.
    assert PIN != TOKEN[:6]


# ── session cookie sign / verify / expiry ────────────────────────────────────

def test_session_roundtrips():
    assert auth.valid_session(auth.make_session()) is True


def test_session_rejects_garbage():
    assert auth.valid_session(None) is False
    assert auth.valid_session("") is False
    assert auth.valid_session("no-dot-here") is False


def test_session_rejects_tampered_signature():
    payload, _, sig = auth.make_session().rpartition(".")
    flipped = "0" if sig[-1] != "0" else "1"
    assert auth.valid_session(f"{payload}.{sig[:-1]}{flipped}") is False


def test_session_rejects_expired(monkeypatch):
    old = auth.time.time() - auth.SESSION_MAX_AGE - 10
    monkeypatch.setattr(auth.time, "time", lambda: old)
    cookie = auth.make_session()
    monkeypatch.undo()  # restore the real clock for verification
    assert auth.valid_session(cookie) is False


# ── secret checks (token or PIN) ──────────────────────────────────────────────

def test_check_secret_accepts_token_and_pin():
    assert auth.check_secret(TOKEN) is True
    assert auth.check_secret(PIN) is True


def test_check_secret_rejects_wrong_and_empty():
    assert auth.check_secret("nope") is False
    assert auth.check_secret("") is False
    assert auth.check_secret(None) is False


# ── FastAPI dependency guards ──────────────────────────────────────────────────

def test_require_bearer_accepts_token_rejects_rest():
    auth.require_bearer(fake_request(headers={"authorization": f"Bearer {TOKEN}"}))  # no raise
    with pytest.raises(HTTPException) as e:
        auth.require_bearer(fake_request(headers={"authorization": "Bearer wrong"}))
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        auth.require_bearer(fake_request())  # missing header


def test_require_session_accepts_cookie_rejects_none():
    good = {auth.SESSION_COOKIE: auth.make_session()}
    auth.require_session(fake_request(cookies=good))  # no raise
    with pytest.raises(HTTPException) as e:
        auth.require_session(fake_request())
    assert e.value.status_code == 401


def test_require_session_or_bearer_accepts_either():
    auth.require_session_or_bearer(fake_request(headers={"authorization": f"Bearer {TOKEN}"}))
    auth.require_session_or_bearer(fake_request(cookies={auth.SESSION_COOKIE: auth.make_session()}))
    with pytest.raises(HTTPException):
        auth.require_session_or_bearer(fake_request())


# ── same-origin guard ──────────────────────────────────────────────────────────

def test_same_origin_allows_missing_origin_and_match():
    auth.require_same_origin(fake_request())  # curl: no Origin → allowed
    auth.require_same_origin(
        fake_request(headers={"origin": "https://h.example", "host": "h.example"})
    )


def test_same_origin_rejects_mismatch():
    with pytest.raises(HTTPException) as e:
        auth.require_same_origin(
            fake_request(headers={"origin": "https://evil.example", "host": "h.example"})
        )
    assert e.value.status_code == 403


# ── login rate-limiter / lockout ───────────────────────────────────────────────

def test_limiter_locks_after_max_fails():
    lim = auth.LoginLimiter(max_fails=3, window=300, lockout=300)
    for _ in range(3):
        assert lim.allowed() is True
        lim.record_fail()
    assert lim.allowed() is False  # locked out after the 3rd failure


def test_limiter_success_clears_lockout():
    lim = auth.LoginLimiter(max_fails=3)
    for _ in range(3):
        lim.record_fail()
    assert lim.allowed() is False
    lim.record_success()
    assert lim.allowed() is True


def test_limiter_drops_failures_outside_window(monkeypatch):
    lim = auth.LoginLimiter(max_fails=3, window=100, lockout=300)
    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    lim.record_fail()
    lim.record_fail()      # 2 failures at t=1000
    now[0] = 1000.0 + 150  # jump past the 100s window
    lim.record_fail()      # the 2 old ones are pruned; only this one counts
    assert lim.allowed() is True
