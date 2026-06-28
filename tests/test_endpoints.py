"""Integration tests for the gateway HTTP surface via FastAPI's TestClient.

SSE broadcasts are asserted through ``client.published`` (the broadcaster's
publish is captured per test) rather than by consuming the live stream.
"""
from __future__ import annotations

from app import auth, store

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
    assert client.get("/api/me").json() == {"authenticated": True}


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
    # Polling registers betty and bumps last_seen → online.
    assert auth_client.post("/api/agent/poll", json={"agent": "betty"}).status_code == 200
    # alfred exists but was last seen long ago → offline.
    store.ensure_agent(conn, "alfred")
    conn.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "alfred"),
    )
    conn.commit()

    agents = {a["id"]: a for a in auth_client.get("/api/agents").json()["agents"]}
    assert agents["betty"]["online"] is True
    assert agents["alfred"]["online"] is False


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
