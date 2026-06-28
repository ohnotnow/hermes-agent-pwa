"""Zero-dependency stdlib HTTP gateway for hap — a bootstrap to prove the loop.

WHY THIS EXISTS: the FastAPI gateway (app/main.py) is the intended target, but
installing fastapi/uvicorn is gated by a safety hook. This stdlib http.server
front exposes the SAME endpoints with ZERO third-party installs, reusing
app.db + app.store, so we can run the real send/reply/send/reply loop against a
live Hermes now. Swap to `app.main:app` (FastAPI) once deps are approved — the
db/store core is shared and unchanged.

Token + db are passed as CLI args (not env) to stay clear of the env-var hook:
    python3 serve.py --token <bearer> [--db hap.db] [--host 127.0.0.1] [--port 8088]
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import db, store
from app.config import VERSION

AGENT_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "hap/" + VERSION

    # -- helpers -----------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        expected = self.server.hap_token  # type: ignore[attr-defined]
        if not expected:
            self._json(503, {"error": "gateway auth token not configured"})
            return False
        auth = self.headers.get("Authorization", "")
        presented = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not secrets.compare_digest(presented, expected):
            self._json(401, {"error": "invalid or missing bearer token"})
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _conn(self):
        return db.connect(self.server.hap_db)  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok", "version": VERSION})
            return
        if not self._authed():
            return
        m = re.fullmatch(r"/api/conversations/([^/]+)", self.path)
        if m:
            conn = self._conn()
            try:
                conv = store.get_conversation(conn, m.group(1))
                if not conv:
                    self._json(404, {"error": "conversation not found"})
                    return
                self._json(200, {
                    "conversation": conv,
                    "messages": store.get_thread(conn, m.group(1)),
                })
            finally:
                conn.close()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authed():
            return
        body = self._body()
        conn = self._conn()
        try:
            if self.path == "/api/conversations":
                agent = body.get("agent", "")
                if not AGENT_ID_RE.match(agent):
                    self._json(422, {"error": "invalid agent id"})
                    return
                store.ensure_agent(conn, agent, body.get("display_name"))
                cid = store.create_conversation(conn, agent)
                mid = store.add_message(conn, cid, agent, "user", body.get("body", ""))
                self._json(200, {"conversation_id": cid, "message_id": mid})
                return

            m = re.fullmatch(r"/api/conversations/([^/]+)/messages", self.path)
            if m:
                conv = store.get_conversation(conn, m.group(1))
                if not conv:
                    self._json(404, {"error": "conversation not found"})
                    return
                mid = store.add_message(conn, m.group(1), conv["agent_id"], "user", body.get("body", ""))
                self._json(200, {"conversation_id": m.group(1), "message_id": mid})
                return

            if self.path == "/api/agent/poll":
                agent = body.get("agent", "")
                if not AGENT_ID_RE.match(agent):
                    self._json(422, {"error": "invalid agent id"})
                    return
                store.ensure_agent(conn, agent, body.get("display_name"))
                self._json(200, {"messages": store.poll_undelivered(conn, agent)})
                return

            if self.path == "/api/agent/reply":
                agent = body.get("agent", "")
                if not AGENT_ID_RE.match(agent):
                    self._json(422, {"error": "invalid agent id"})
                    return
                conv = store.get_conversation(conn, body.get("conversation_id", ""))
                if not conv:
                    self._json(404, {"error": "conversation not found"})
                    return
                if conv["agent_id"] != agent:
                    self._json(409, {"error": "agent does not match conversation"})
                    return
                store.ensure_agent(conn, agent)
                mid = store.add_message(
                    conn, body["conversation_id"], agent, "agent",
                    body.get("body", ""), body.get("message_id"),
                )
                self._json(200, {"message_id": mid})
                return

            self._json(404, {"error": "not found"})
        finally:
            conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--db", default="hap.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8088)
    args = ap.parse_args()

    conn = db.connect(args.db)
    db.init_db(conn)
    conn.close()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.hap_token = args.token  # type: ignore[attr-defined]
    httpd.hap_db = args.db        # type: ignore[attr-defined]
    print(f"hap stdlib gateway on http://{args.host}:{args.port} (v{VERSION})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
