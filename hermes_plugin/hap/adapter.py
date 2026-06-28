"""hap platform adapter for Hermes — bridges a Hermes agent to the hap gateway.

Inbound: polls the hap gateway for user messages and dispatches them to the
agent via self.handle_message(). Outbound: send() POSTs the agent's reply back
to the gateway. To the agent, this is just another chat platform.

Verified contract: see the hap repo's ant notes (hap-VYQvH, hap-sYVTv).

Config (env first, then hap.json beside this file):
  HAP_GATEWAY_URL    e.g. http://127.0.0.1:8088
  HAP_GATEWAY_TOKEN  bearer token for the gateway
  HAP_AGENT_ID       this agent's id on the gateway (e.g. betty)
  HAP_POLL_SECONDS   poll interval (default 3)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path(__file__).with_name("hap.json")


def _load_cfg() -> dict:
    data: dict = {}
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return {
        "gateway_url": os.getenv("HAP_GATEWAY_URL") or data.get("gateway_url", "http://127.0.0.1:8088"),
        "token": os.getenv("HAP_GATEWAY_TOKEN") or data.get("token", ""),
        "agent_id": os.getenv("HAP_AGENT_ID") or data.get("agent_id", "betty"),
        "poll_seconds": float(os.getenv("HAP_POLL_SECONDS") or data.get("poll_seconds", 3)),
    }


class HapAdapter(BasePlatformAdapter):
    # Persistent outbound channel: send() can always POST to the gateway, so the
    # agent MAY deliver later/unprompted updates. (base sets this False only for
    # the stateless api_server — we are emphatically not that.)
    supports_async_delivery = True

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("hap"))
        # Declare a home channel so Hermes stops prompting "/sethome" at the
        # start of every new conversation (gateway/run.py only checks this env
        # var is set). We don't do cron/cross-platform delivery in v1, so the
        # value is a placeholder. Plugins may set os.environ per Hermes' docs.
        os.environ.setdefault("HAP_HOME_CHANNEL", "home")
        cfg = _load_cfg()
        self._base_url = cfg["gateway_url"].rstrip("/")
        self._token = cfg["token"]
        self._agent_id = cfg["agent_id"]
        self._poll_seconds = cfg["poll_seconds"]
        self._client: Optional[httpx.AsyncClient] = None
        self._poll_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "hap"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._token:
            self._set_fatal_error("config_missing", "hap token not configured", retryable=False)
            return False
        self._client = httpx.AsyncClient(timeout=30.0)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        logger.info("hap: connected; polling %s as agent '%s'", self._base_url, self._agent_id)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── outbound ──────────────────────────────────────────────────────────

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="not connected")
        try:
            r = await self._client.post(
                f"{self._base_url}/api/agent/reply",
                headers=self._headers(),
                json={
                    "agent": self._agent_id,
                    "conversation_id": chat_id,
                    "body": content,
                    "message_id": f"msg_{uuid.uuid4().hex[:20]}",
                },
            )
            if r.status_code == 200:
                return SendResult(success=True, message_id=r.json().get("message_id"))
            return SendResult(success=False, error=f"gateway {r.status_code}: {r.text}")
        except Exception as e:  # noqa: BLE001 — surface as a retryable send failure
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    # ── inbound (poll loop) ───────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        assert self._client is not None
        while True:
            try:
                r = await self._client.post(
                    f"{self._base_url}/api/agent/poll",
                    headers=self._headers(),
                    json={"agent": self._agent_id},
                )
                if r.status_code == 200:
                    for m in r.json().get("messages", []):
                        await self._dispatch(m)
                else:
                    logger.warning("hap: poll got %s: %s", r.status_code, r.text)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("hap: poll error: %s", e)
            await asyncio.sleep(self._poll_seconds)

    async def _dispatch(self, m: dict) -> None:
        # One trusted human in v1; the bearer token is the authorization, so the
        # source is marked role_authorized to satisfy the gateway's access gate.
        source = self.build_source(
            chat_id=m["conversation_id"],
            chat_type="dm",
            user_id="hap-user",
            user_name="User",
            role_authorized=True,
        )
        event = MessageEvent(text=m.get("body", ""), source=source, message_id=m.get("message_id"))
        await self.handle_message(event)


def register(ctx):
    ctx.register_platform(
        name="hap",
        label="Hap",
        adapter_factory=lambda cfg: HapAdapter(cfg),
        check_fn=lambda: True,
        emoji="📱",
    )
