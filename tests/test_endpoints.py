"""Integration tests for the gateway HTTP surface via FastAPI's TestClient.

SSE broadcasts are asserted through ``client.published`` (the broadcaster's
publish is captured per test) rather than by consuming the live stream.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app import auth, main, store

TOKEN = auth.settings.auth_token


# ── health ────────────────────────────────────────────────────────────────────

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── login / me / logout ────────────────────────────────────────────────────────

def test_login_with_token_then_me(client):
    r = client.post("/api/login", json={"secret": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert client.cookies.get(auth.SESSION_COOKIE)
    assert client.get("/api/me").json() == {"authenticated": True, "browser_auth": True}


def test_me_renews_an_existing_session_cookie(client):
    client.post("/api/login", json={"secret": TOKEN})
    assert auth.SESSION_COOKIE in client.get("/api/me").headers.get("set-cookie", "")


def test_me_does_not_issue_a_cookie_without_a_session(client):
    r = client.get("/api/me")
    assert r.json()["authenticated"] is False
    assert "set-cookie" not in r.headers


def test_login_with_pin(client):
    r = client.post("/api/login", json={"secret": auth.PIN})
    assert r.status_code == 200
    assert client.get("/api/me").json()["authenticated"] is True


def test_login_wrong_secret_401(client):
    assert client.post("/api/login", json={"secret": "wrong"}).status_code == 401
    assert client.get("/api/me").json()["authenticated"] is False


def test_logout_clears_session(client):
    client.post("/api/login", json={"secret": TOKEN})
    assert client.get("/api/me").json()["authenticated"] is True
    client.post("/api/logout")
    assert client.get("/api/me").json()["authenticated"] is False


def test_login_locks_out_after_repeated_failures(client):
    for _ in range(5):
        assert client.post("/api/login", json={"secret": "wrong"}).status_code == 401
    # The limiter is now tripped: even the correct secret is refused.
    assert client.post("/api/login", json={"secret": "wrong"}).status_code == 429
    assert client.post("/api/login", json={"secret": TOKEN}).status_code == 429


def test_login_rejects_cross_origin(client):
    r = client.post(
        "/api/login",
        json={"secret": TOKEN},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


# ── browser auth off (the default) ────────────────────────────────────────────

def test_browser_auth_off_opens_browser_endpoints(client, browser_auth_off):
    assert client.get("/api/me").json() == {"authenticated": True, "browser_auth": False}
    assert client.get("/api/agents").status_code == 200
    r = client.post("/api/conversations", json={"agent": "betty", "body": "hi"})
    assert r.status_code == 200


def test_browser_auth_off_still_guards_agents_and_cross_origin(client, browser_auth_off):
    assert client.post("/api/agent/poll", json={"agent": "betty"}).status_code == 401
    r = client.post(
        "/api/conversations",
        json={"agent": "betty", "body": "hi"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


# ── auth required ───────────────────────────────────────────────────────────────

def test_endpoints_require_auth_401(client):
    assert client.get("/api/agents").status_code == 401
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/agent/poll", json={"agent": "betty"}).status_code == 401


def test_events_stream_needs_a_session_not_just_bearer(client):
    # /api/events is session-only; a bearer token is not enough. Auth fails
    # before the (infinite) stream starts, so this returns promptly.
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    assert client.get("/api/events").status_code == 401


# ── agents online flag ───────────────────────────────────────────────────────

def test_agents_online_flag(auth_client, conn):
    # Polling registers betty and marks her seen → online.
    assert auth_client.post("/api/agent/poll", json={"agent": "betty"}).status_code == 200
    # alfred is registered but has not polled since the gateway started → offline.
    store.ensure_agent(conn, "alfred")

    agents = {a["id"]: a for a in auth_client.get("/api/agents").json()["agents"]}
    assert agents["betty"]["online"] is True
    assert agents["alfred"]["online"] is False
    assert "last_seen_at" not in agents["betty"]

    # betty goes quiet for longer than the window → offline.
    main._last_seen["betty"] -= main.ONLINE_WINDOW_SECONDS + 1
    agents = {a["id"]: a for a in auth_client.get("/api/agents").json()["agents"]}
    assert agents["betty"]["online"] is False


def test_poll_does_not_write_to_db_after_first_contact(auth_client, conn):
    auth_client.post("/api/agent/poll", json={"agent": "betty"})
    before = conn.total_changes
    for _ in range(3):
        auth_client.post("/api/agent/poll", json={"agent": "betty"})
    assert conn.total_changes == before


# ── conversation lifecycle ───────────────────────────────────────────────────

def test_start_reply_and_detail_flow(auth_client, tick):
    r = auth_client.post("/api/conversations", json={"agent": "betty", "body": "hello betty"})
    assert r.status_code == 200
    cid = r.json()["conversation_id"]

    # Starting a conversation broadcasts the user's message.
    assert auth_client.published[-1]["type"] == "message"
    assert auth_client.published[-1]["conversation_id"] == cid

    assert any(c["id"] == cid for c in auth_client.get("/api/conversations").json()["conversations"])

    r = auth_client.post(
        "/api/agent/reply",
        json={"agent": "betty", "conversation_id": cid, "body": "hi human"},
    )
    assert r.status_code == 200
    assert auth_client.published[-1] == {
        "type": "message",
        "conversation_id": cid,
        "sender": "agent",
        "body": "hi human",
        "message_id": r.json()["message_id"],
    }

    detail = auth_client.get(f"/api/conversations/{cid}").json()
    assert [(m["sender"], m["body"]) for m in detail["messages"]] == [
        ("user", "hello betty"),
        ("agent", "hi human"),
    ]


def test_user_reply_appends_and_broadcasts(auth_client):
    cid = auth_client.post(
        "/api/conversations", json={"agent": "betty", "body": "first"}
    ).json()["conversation_id"]
    auth_client.published.clear()

    r = auth_client.post(f"/api/conversations/{cid}/messages", json={"body": "second"})
    assert r.status_code == 200
    assert auth_client.published[-1]["type"] == "message"
    assert auth_client.published[-1]["body"] == "second"


def test_user_reply_unknown_conversation_404(auth_client):
    assert auth_client.post(
        "/api/conversations/conv_missing/messages", json={"body": "x"}
    ).status_code == 404


def test_invalid_agent_id_rejected_422(auth_client):
    assert auth_client.post(
        "/api/conversations", json={"agent": "Bad Agent!", "body": "x"}
    ).status_code == 422


# ── agent poll / reply ─────────────────────────────────────────────────────────

def test_agent_poll_delivers_once_and_broadcasts_delivered(auth_client):
    cid = auth_client.post(
        "/api/conversations", json={"agent": "betty", "body": "ping"}
    ).json()["conversation_id"]
    auth_client.published.clear()

    msgs = auth_client.post("/api/agent/poll", json={"agent": "betty"}).json()["messages"]
    assert [m["body"] for m in msgs] == ["ping"]

    delivered = [e for e in auth_client.published if e["type"] == "delivered"]
    assert delivered and delivered[0]["conversation_id"] == cid

    # Second poll: already delivered, nothing returned.
    assert auth_client.post("/api/agent/poll", json={"agent": "betty"}).json()["messages"] == []


def test_agent_reply_mismatched_agent_409(auth_client):
    cid = auth_client.post(
        "/api/conversations", json={"agent": "betty", "body": "x"}
    ).json()["conversation_id"]
    r = auth_client.post(
        "/api/agent/reply",
        json={"agent": "alfred", "conversation_id": cid, "body": "not mine"},
    )
    assert r.status_code == 409


def test_agent_reply_unknown_conversation_404(auth_client):
    r = auth_client.post(
        "/api/agent/reply",
        json={"agent": "betty", "conversation_id": "conv_missing", "body": "x"},
    )
    assert r.status_code == 404


# ── delete + un-hide ───────────────────────────────────────────────────────────

def test_delete_broadcasts_and_agent_reply_unhides(auth_client):
    cid = auth_client.post(
        "/api/conversations", json={"agent": "betty", "body": "hi"}
    ).json()["conversation_id"]
    auth_client.published.clear()

    assert auth_client.delete(f"/api/conversations/{cid}").status_code == 200
    assert {"type": "deleted", "conversation_id": cid} in auth_client.published
    assert all(c["id"] != cid for c in auth_client.get("/api/conversations").json()["conversations"])

    # A late agent reply resurfaces the hidden conversation (async-by-default).
    auth_client.post(
        "/api/agent/reply",
        json={"agent": "betty", "conversation_id": cid, "body": "late reply"},
    )
    assert any(c["id"] == cid for c in auth_client.get("/api/conversations").json()["conversations"])


def test_delete_unknown_conversation_404(auth_client):
    assert auth_client.delete("/api/conversations/conv_missing").status_code == 404


# ── long-poll ───────────────────────────────────────────────────────────────

@pytest.fixture
def anyio_backend():
    return "asyncio"


def _async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


@pytest.mark.anyio
async def test_held_poll_wakes_when_a_message_arrives(auth_client):
    async with _async_client() as ac:
        poll = asyncio.create_task(ac.post("/api/agent/poll", json={"agent": "betty", "wait": 10}))
        await asyncio.sleep(0.2)
        assert not poll.done()  # nothing waiting, so the gateway is holding it

        await ac.post("/api/conversations", json={"agent": "betty", "body": "hello"})
        r = await asyncio.wait_for(poll, timeout=2)  # well before the 10s hold
    assert [m["body"] for m in r.json()["messages"]] == ["hello"]


@pytest.mark.anyio
async def test_held_poll_ignores_other_agents_messages(auth_client):
    async with _async_client() as ac:
        poll = asyncio.create_task(ac.post("/api/agent/poll", json={"agent": "betty", "wait": 0.5}))
        await asyncio.sleep(0.1)
        await ac.post("/api/conversations", json={"agent": "alfred", "body": "not for betty"})
        r = await poll
    assert r.json()["messages"] == []


@pytest.mark.anyio
async def test_held_poll_times_out_empty(auth_client):
    async with _async_client() as ac:
        r = await asyncio.wait_for(
            ac.post("/api/agent/poll", json={"agent": "betty", "wait": 0.2}), timeout=2
        )
    assert r.json()["messages"] == []
    assert main._poll_waiters == {}


def test_poll_access_log_is_filtered():
    def record(path):
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1234", "POST", path, "1.1", 200), None,
        )

    f = main._SkipPollAccessLog()
    assert f.filter(record("/api/agent/poll")) is False
    assert f.filter(record("/api/agent/reply")) is True
