"""Unit tests for app.store — the SQLite data-access helpers, driven directly
against an in-memory connection (no app, no HTTP).

Foreign keys are ON, so each test builds the agent → conversation → message
chain in order.
"""
from __future__ import annotations

from app import store


def _seed_conv(conn, agent="betty"):
    store.ensure_agent(conn, agent)
    return store.create_conversation(conn, agent)


# ── agents ────────────────────────────────────────────────────────────────────

def test_ensure_agent_inserts_then_upserts(conn):
    store.ensure_agent(conn, "betty", "Betty")
    agents = store.list_agents(conn)
    assert len(agents) == 1
    assert (agents[0]["id"], agents[0]["display_name"]) == ("betty", "Betty")

    # Second contact with no name: it's an upsert (one row), name preserved.
    store.ensure_agent(conn, "betty")
    agents = store.list_agents(conn)
    assert len(agents) == 1
    assert agents[0]["display_name"] == "Betty"


# ── conversations ───────────────────────────────────────────────────────────

def test_create_and_get_conversation(conn):
    store.ensure_agent(conn, "betty")
    cid = store.create_conversation(conn, "betty", title="hi")
    conv = store.get_conversation(conn, cid)
    assert conv["id"] == cid
    assert conv["agent_id"] == "betty"
    assert conv["deleted_at"] is None
    assert store.get_conversation(conn, "nope") is None


# ── messages + dedupe ─────────────────────────────────────────────────────────

def test_add_message_dedupes_on_message_id(conn):
    cid = _seed_conv(conn)
    mid = store.add_message(conn, cid, "betty", "user", "hello", message_id="m1")
    again = store.add_message(conn, cid, "betty", "user", "different body", message_id="m1")
    assert again == "m1"
    thread = store.get_thread(conn, cid)
    assert len(thread) == 1            # the replay was ignored
    assert thread[0]["body"] == "hello"  # first write wins


def test_get_thread_orders_chronologically(conn, tick):
    cid = _seed_conv(conn)
    store.add_message(conn, cid, "betty", "user", "first")
    store.add_message(conn, cid, "betty", "agent", "second")
    bodies = [m["body"] for m in store.get_thread(conn, cid)]
    assert bodies == ["first", "second"]


# ── poll cursor (deliver-once) ─────────────────────────────────────────────────

def test_poll_undelivered_delivers_once(conn, tick):
    cid = _seed_conv(conn)
    store.add_message(conn, cid, "betty", "user", "one")
    store.add_message(conn, cid, "betty", "user", "two")
    store.add_message(conn, cid, "betty", "agent", "not a user message")

    first = store.poll_undelivered(conn, "betty")
    assert [m["body"] for m in first] == ["one", "two"]  # user msgs only, in order
    assert store.poll_undelivered(conn, "betty") == []   # marked delivered → no re-poll


def test_poll_undelivered_is_scoped_per_agent(conn):
    cb = _seed_conv(conn, "betty")
    store.ensure_agent(conn, "alfred")
    ca = store.create_conversation(conn, "alfred")
    store.add_message(conn, cb, "betty", "user", "for betty")
    store.add_message(conn, ca, "alfred", "user", "for alfred")
    assert [m["body"] for m in store.poll_undelivered(conn, "betty")] == ["for betty"]


# ── soft-delete / un-hide ──────────────────────────────────────────────────────

def test_soft_delete_hides_and_unhide_restores(conn):
    cid = _seed_conv(conn)
    store.add_message(conn, cid, "betty", "user", "hi")
    assert any(c["id"] == cid for c in store.list_conversations(conn))

    assert store.set_conversation_deleted(conn, cid, True) is True
    assert all(c["id"] != cid for c in store.list_conversations(conn))  # hidden

    assert store.set_conversation_deleted(conn, cid, False) is True
    assert any(c["id"] == cid for c in store.list_conversations(conn))  # restored

    assert store.set_conversation_deleted(conn, "missing", True) is False


def test_list_conversations_preview_and_activity_order(conn, tick):
    store.ensure_agent(conn, "betty")
    c1 = store.create_conversation(conn, "betty")
    c2 = store.create_conversation(conn, "betty")
    store.add_message(conn, c1, "betty", "user", "older")
    store.add_message(conn, c2, "betty", "user", "newer")

    convs = store.list_conversations(conn)
    assert convs[0]["id"] == c2  # newest activity first
    assert convs[0]["last_body"] == "newer"
    assert convs[0]["last_sender"] == "user"
