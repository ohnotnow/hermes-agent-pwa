"""SQLite persistence for the hap gateway.

SQLite is the durable source of truth — no message broker (see ant hap-VYQvH).
Stdlib sqlite3 only, so no extra dependency.

Delivery model (QUESTIONS #3 lean): each message has a primary-key message_id
for dedupe, and user messages carry delivered_to_agent_at — NULL until the
agent's adapter has polled and acked them. That is the durable inbox cursor.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    display_name  TEXT,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    title       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id            TEXT PRIMARY KEY,       -- dedupe key
    conversation_id       TEXT NOT NULL REFERENCES conversations(id),
    agent_id              TEXT NOT NULL,
    sender                TEXT NOT NULL CHECK (sender IN ('user', 'agent')),
    body                  TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    delivered_to_agent_at TEXT                    -- NULL until polled by the agent
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_undelivered
    ON messages(agent_id, delivered_to_agent_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
