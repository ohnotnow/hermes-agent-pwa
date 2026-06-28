"""Shared pytest fixtures for the hap gateway.

CRITICAL ordering note: ``app.auth`` derives the PIN and the session-signing
key from the auth token *at import time*, and ``app.main`` captures the db_path
at import time too. So we pin the environment **before** any ``app.*`` module is
imported. pytest imports conftest.py before the test modules, so doing it here
at module top is enough — the test files can import ``app`` freely.
"""
from __future__ import annotations

import os

# Force a known, deterministic config for the whole test process. (Assignment,
# not setdefault — we don't want a developer's real shell token leaking in.)
os.environ["HAP_AUTH_TOKEN"] = "hap-test-token-0123456789abcdef"
os.environ["HAP_DB_PATH"] = ":memory:"
os.environ.pop("HAP_COOKIE_SECURE", None)  # default false → cookies work over test http

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, main  # noqa: E402

TEST_TOKEN = os.environ["HAP_AUTH_TOKEN"]


@pytest.fixture
def conn():
    """A fresh in-memory SQLite connection with the schema applied.

    One connection per test (``:memory:`` is private to its connection), so
    tests are fully isolated.
    """
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def client(conn, monkeypatch):
    """A TestClient backed by the throwaway ``conn``.

    We deliberately do *not* use TestClient as a context manager, so the app
    lifespan (which would open the configured db_path on disk) never runs — we
    inject our in-memory connection onto ``app.state.db`` instead. The module
    level broadcaster and login limiter are singletons, so we reset/capture them
    per test to stop state leaking between tests.

    ``client.published`` collects every event the handlers broadcast, so tests
    can assert SSE behaviour without consuming the live event stream.
    """
    main.app.state.db = conn
    auth.login_limiter.record_success()  # reset the singleton limiter

    published: list[dict] = []
    monkeypatch.setattr(main.broadcaster, "publish", published.append)

    tc = TestClient(main.app)
    tc.published = published
    return tc


@pytest.fixture
def auth_client(client):
    """A client pre-authenticated with the bearer token. Covers every
    session-or-bearer endpoint; agent endpoints require the bearer anyway."""
    client.headers["Authorization"] = f"Bearer {TEST_TOKEN}"
    return client


@pytest.fixture
def tick(monkeypatch):
    """Make ``store._now()`` return strictly increasing timestamps so tests that
    assert ordering (thread order, conversation list, poll order) are
    deterministic rather than relying on wall-clock microsecond resolution.

    Not autouse: timestamps are far in the past, which would make every agent
    look offline — tests of the online flag must use the real clock.
    """
    from app import store

    counter = {"n": 0}

    def _now() -> str:
        counter["n"] += 1
        return f"2026-01-01T00:00:00.{counter['n']:06d}+00:00"

    monkeypatch.setattr(store, "_now", _now)
    return counter
