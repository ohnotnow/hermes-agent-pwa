"""hap gateway — FastAPI app.

A small self-hosted gateway between a phone PWA and local Hermes agents.
SQLite is the durable store; no message broker. See ant hap-AkRXV / hap-VYQvH.

Run: uvicorn app.main:app --host 127.0.0.1 --port 8088

Auth model:
  - Agent endpoints (poll/reply): raw bearer token (the agent holds it).
  - Browser endpoints: signed session cookie from /api/login (token or PIN),
    or the bearer token (for curl/scripts). The browser never stores the token.
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import auth, db, store
from app.config import VERSION, load_settings
from app.events import Broadcaster

AGENT_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# An agent is "online" if it has polled within this window (it polls ~every 3s).
ONLINE_WINDOW_SECONDS = 15

settings = load_settings()
broadcaster = Broadcaster()


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

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.json")
async def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/offline.html")
async def offline() -> FileResponse:
    return FileResponse(STATIC_DIR / "offline.html")


def valid_agent(agent_id: str) -> str:
    if not AGENT_ID_RE.match(agent_id or ""):
        raise HTTPException(422, "invalid agent id (must match ^[a-z0-9_-]{1,32}$)")
    return agent_id


def _agent_online(last_seen_at: str | None) -> bool:
    if not last_seen_at:
        return False
    try:
        seen = datetime.fromisoformat(last_seen_at)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - seen).total_seconds() <= ONLINE_WINDOW_SECONDS


def _publish_message(conversation_id: str, sender: str, body: str, message_id: str) -> None:
    broadcaster.publish({
        "type": "message",
        "conversation_id": conversation_id,
        "sender": sender,
        "body": body,
        "message_id": message_id,
    })


# ── request bodies ───────────────────────────────────────────────────────

class Login(BaseModel):
    secret: str


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


# ── auth ─────────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(payload: Login, request: Request, response: Response) -> dict:
    auth.require_same_origin(request)
    if not auth.login_limiter.allowed():
        raise HTTPException(429, "too many attempts — locked out, try again shortly")
    if not auth.check_secret(payload.secret):
        auth.login_limiter.record_fail()
        raise HTTPException(401, "invalid secret")
    auth.login_limiter.record_success()
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_session(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request) -> dict:
    return {"authenticated": auth.is_authed(request)}


# ── user (phone) side ────────────────────────────────────────────────────

@app.get("/api/agents", dependencies=[Depends(auth.require_session_or_bearer)])
async def agents(request: Request) -> dict:
    items = store.list_agents(request.app.state.db)
    for a in items:
        a["online"] = _agent_online(a.get("last_seen_at"))
    return {"agents": items}


@app.get("/api/conversations", dependencies=[Depends(auth.require_session_or_bearer)])
async def conversations(request: Request) -> dict:
    return {"conversations": store.list_conversations(request.app.state.db)}


@app.post(
    "/api/conversations",
    dependencies=[Depends(auth.require_session_or_bearer), Depends(auth.require_same_origin)],
)
async def start_conversation(payload: StartConversation, request: Request) -> dict:
    conn = request.app.state.db
    agent = valid_agent(payload.agent)
    store.ensure_agent(conn, agent, payload.display_name)
    cid = store.create_conversation(conn, agent)
    mid = store.add_message(conn, cid, agent, "user", payload.body)
    _publish_message(cid, "user", payload.body, mid)
    return {"conversation_id": cid, "message_id": mid}


@app.post(
    "/api/conversations/{conversation_id}/messages",
    dependencies=[Depends(auth.require_session_or_bearer), Depends(auth.require_same_origin)],
)
async def user_reply(conversation_id: str, payload: UserReply, request: Request) -> dict:
    conn = request.app.state.db
    conv = store.get_conversation(conn, conversation_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    mid = store.add_message(conn, conversation_id, conv["agent_id"], "user", payload.body)
    _publish_message(conversation_id, "user", payload.body, mid)
    return {"conversation_id": conversation_id, "message_id": mid}


@app.get(
    "/api/conversations/{conversation_id}",
    dependencies=[Depends(auth.require_session_or_bearer)],
)
async def conversation_detail(conversation_id: str, request: Request) -> dict:
    conn = request.app.state.db
    conv = store.get_conversation(conn, conversation_id)
    if not conv:
        raise HTTPException(404, "conversation not found")
    return {"conversation": conv, "messages": store.get_thread(conn, conversation_id)}


@app.delete(
    "/api/conversations/{conversation_id}",
    dependencies=[Depends(auth.require_session_or_bearer), Depends(auth.require_same_origin)],
)
async def delete_conversation(conversation_id: str, request: Request) -> dict:
    conn = request.app.state.db
    if not store.set_conversation_deleted(conn, conversation_id, True):
        raise HTTPException(404, "conversation not found")
    broadcaster.publish({"type": "deleted", "conversation_id": conversation_id})
    return {"ok": True}


# ── live updates (SSE) ───────────────────────────────────────────────────

@app.get("/api/events", dependencies=[Depends(auth.require_session)])
async def events_stream(request: Request) -> StreamingResponse:
    queue = await broadcaster.subscribe()

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(evt)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── agent (adapter) side — bearer token only ─────────────────────────────

@app.post("/api/agent/poll", dependencies=[Depends(auth.require_bearer)])
async def agent_poll(payload: AgentPoll, request: Request) -> dict:
    conn = request.app.state.db
    agent = valid_agent(payload.agent)
    store.ensure_agent(conn, agent, payload.display_name)  # auto-register on first contact
    msgs = store.poll_undelivered(conn, agent)
    # Let connected browsers tick "delivered" once their messages reach the agent.
    by_conv: dict[str, list[str]] = {}
    for m in msgs:
        by_conv.setdefault(m["conversation_id"], []).append(m["message_id"])
    for cid, ids in by_conv.items():
        broadcaster.publish({"type": "delivered", "conversation_id": cid, "message_ids": ids})
    return {"messages": msgs}


@app.post("/api/agent/reply", dependencies=[Depends(auth.require_bearer)])
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
    # A late reply to a hidden conversation resurfaces it (async-by-default).
    store.set_conversation_deleted(conn, payload.conversation_id, False)
    _publish_message(payload.conversation_id, "agent", payload.body, mid)
    return {"message_id": mid}
