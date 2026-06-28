"""Data-access helpers for the hap gateway (stdlib sqlite3).

Pure functions over a sqlite3 connection. The gateway holds one connection on
app.state; these helpers keep the route handlers thin.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def ensure_agent(conn: sqlite3.Connection, agent_id: str, display_name: str | None = None) -> None:
    """Auto-register an agent on first contact; bump last_seen otherwise."""
    now = _now()
    conn.execute(
        """INSERT INTO agents (id, display_name, created_at, last_seen_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               last_seen_at = excluded.last_seen_at,
               display_name = COALESCE(excluded.display_name, agents.display_name)""",
        (agent_id, display_name, now, now),
    )
    conn.commit()


def create_conversation(conn: sqlite3.Connection, agent_id: str, title: str | None = None) -> str:
    cid = _new_id("conv")
    conn.execute(
        "INSERT INTO conversations (id, agent_id, title, created_at) VALUES (?, ?, ?, ?)",
        (cid, agent_id, title, _now()),
    )
    conn.commit()
    return cid


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    return dict(row) if row else None


def add_message(
    conn: sqlite3.Connection,
    conversation_id: str,
    agent_id: str,
    sender: str,
    body: str,
    message_id: str | None = None,
) -> str:
    """Insert a message, idempotent on message_id (dedupe on replay)."""
    mid = message_id or _new_id("msg")
    try:
        conn.execute(
            """INSERT INTO messages
               (message_id, conversation_id, agent_id, sender, body, created_at, delivered_to_agent_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (mid, conversation_id, agent_id, sender, body, _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already stored — idempotent
    return mid


def poll_undelivered(conn: sqlite3.Connection, agent_id: str) -> list[dict]:
    """Return undelivered user messages for an agent and mark them delivered.

    v1 = mark-on-poll (at-most-once). A future hardening (QUESTIONS #3) is to
    defer the mark until the adapter explicitly acks, for at-least-once.
    """
    rows = conn.execute(
        """SELECT message_id, conversation_id, body, created_at
           FROM messages
           WHERE agent_id = ? AND sender = 'user' AND delivered_to_agent_at IS NULL
           ORDER BY created_at ASC""",
        (agent_id,),
    ).fetchall()
    msgs = [dict(r) for r in rows]
    if msgs:
        now = _now()
        conn.executemany(
            "UPDATE messages SET delivered_to_agent_at = ? WHERE message_id = ?",
            [(now, m["message_id"]) for m in msgs],
        )
        conn.commit()
    return msgs


def get_thread(conn: sqlite3.Connection, conversation_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT message_id, sender, body, created_at, delivered_to_agent_at
           FROM messages WHERE conversation_id = ? ORDER BY created_at ASC""",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_agents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, display_name, created_at, last_seen_at FROM agents ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def list_conversations(conn: sqlite3.Connection) -> list[dict]:
    """Conversations with a last-message preview, newest activity first."""
    rows = conn.execute(
        """SELECT c.id, c.agent_id, c.title, c.created_at,
                  m.body   AS last_body,
                  m.sender AS last_sender,
                  m.created_at AS last_at
           FROM conversations c
           LEFT JOIN messages m ON m.message_id = (
               SELECT message_id FROM messages
               WHERE conversation_id = c.id
               ORDER BY created_at DESC LIMIT 1
           )
           ORDER BY COALESCE(m.created_at, c.created_at) DESC"""
    ).fetchall()
    return [dict(r) for r in rows]
