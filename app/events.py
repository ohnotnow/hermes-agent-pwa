"""In-process SSE broadcaster for live updates to connected browsers.

No message broker: a new message is published to every connected SSE client's
asyncio queue. Single-process only (we run one uvicorn worker); a multi-worker
deployment would need a shared bus, which v1 does not.
"""
from __future__ import annotations

import asyncio


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(event)
