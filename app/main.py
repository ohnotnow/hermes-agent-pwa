"""hap gateway — FastAPI app.

A small self-hosted gateway between a phone PWA and local Hermes agents.
SQLite is the durable store; no message broker. See ant hap-AkRXV / hap-VYQvH.

Run: uvicorn app.main:app --host 127.0.0.1 --port 8088

Auth: one shared bearer token (HAP_AUTH_TOKEN) for both the human and the
agents. Agent endpoints auto-register an agent on first contact.

Endpoints (all require the bearer token):
  POST /api/conversations                      start a conversation with an agent
  POST /api/conversations/{id}/messages        user reply in a conversation
  GET  /api/conversations/{id}                 fetch the full thread
  POST /api/agent/poll                         adapter: fetch undelivered user msgs
  POST /api/agent/reply                        adapter: post the agent's reply
"""
from __future__ import annotations

import re
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from app import db, store
from app.config import VERSION, load_settings

AGENT_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    app.state.db = conn
    try:
        yield
    finally:
        conn.close()


app = FastAPI(title="hap gateway", version=VERSION, lifespan=lifespan)


# ── auth + validation ────────────────────────────────────────────────────

def require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = settings.auth_token
    if not expected:
        raise HTTPException(503, "gateway auth token not configured (set HAP_AUTH_TOKEN)")
    presented = ""
    if authorization and authorization.startswith("Bearer "):
        presented = authorization[len("Bearer "):]
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(401, "invalid or missing bearer token")


def valid_agent(agent_id: str) -> str:
    if not AGENT_ID_RE.match(agent_id or ""):
        raise HTTPException(422, "invalid agent id (must match ^[a-z0-9_-]{1,32}$)")
    return agent_id


# ── request bodies ───────────────────────────────────────────────────────

class StartConversation(BaseModel):
    agent: str
    body: str
    display_name: str | None = None


class UserReply(BaseModel):
    body: str


class AgentPoll(BaseModel):
    agent: str
    display_name: str | None = None


class AgentReply(BaseModel):
    agent: str
    conversation_id: str
    body: str
    message_id: str | None = None


# ── health ───────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "version": VERSION}


# ── user (phone) side ────────────────────────────────────────────────────

@app.post("/api/conversations", dependencies=[Depends(require_auth)])
async def start_conversation(payload: StartConversation, request: Request) -> dict:
    conn = request.app.state.db
    agent = valid_agent(payload.agent)
    store.ensure_agent(conn, agent, payload.display_name)
    cid = store.create_conversation(conn, agent)
    mid = store.add_message(conn, cid, agent, "user", payload.body)
    return {"conversation_id": cid, "message_id": mid}


@app.post("/api/conversations/{conversation_id}/messages", dependencies=[Depends(require_auth)])
async def user_reply(conversation_id: str, payload: UserReply, request: Request) -> dict:
    conn = request.app.state.db
    conv = store.get_conversation(conn, conversation_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    mid = store.add_message(conn, conversation_id, conv["agent_id"], "user", payload.body)
    return {"conversation_id": conversation_id, "message_id": mid}


@app.get("/api/conversations/{conversation_id}", dependencies=[Depends(require_auth)])
async def conversation_detail(conversation_id: str, request: Request) -> dict:
    conn = request.app.state.db
    conv = store.get_conversation(conn, conversation_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    return {"conversation": conv, "messages": store.get_thread(conn, conversation_id)}


# ── agent (adapter) side ─────────────────────────────────────────────────

@app.post("/api/agent/poll", dependencies=[Depends(require_auth)])
async def agent_poll(payload: AgentPoll, request: Request) -> dict:
    conn = request.app.state.db
    agent = valid_agent(payload.agent)
    store.ensure_agent(conn, agent, payload.display_name)  # auto-register on first contact
    return {"messages": store.poll_undelivered(conn, agent)}


@app.post("/api/agent/reply", dependencies=[Depends(require_auth)])
async def agent_reply(payload: AgentReply, request: Request) -> dict:
    conn = request.app.state.db
    agent = valid_agent(payload.agent)
    conv = store.get_conversation(conn, payload.conversation_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    if conv["agent_id"] != agent:
        raise HTTPException(409, "agent does not match conversation")
    store.ensure_agent(conn, agent)
    mid = store.add_message(conn, payload.conversation_id, agent, "agent", payload.body, payload.message_id)
    return {"message_id": mid}
