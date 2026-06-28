"""Unit tests for the in-process SSE Broadcaster.

Exercises the real subscribe/publish/unsubscribe mechanism directly (no HTTP),
so we never have to consume the live, infinite event stream.
"""
from __future__ import annotations

import asyncio

from app.events import Broadcaster


def test_broadcast_fans_out_to_all_subscribers():
    async def scenario():
        b = Broadcaster()
        q1, q2 = await b.subscribe(), await b.subscribe()

        b.publish({"type": "message", "id": "m1"})
        assert q1.get_nowait() == {"type": "message", "id": "m1"}
        assert q2.get_nowait() == {"type": "message", "id": "m1"}

        b.unsubscribe(q1)
        b.publish({"type": "deleted"})
        assert q2.get_nowait() == {"type": "deleted"}
        assert q1.empty()  # unsubscribed → receives nothing further

    asyncio.run(scenario())
