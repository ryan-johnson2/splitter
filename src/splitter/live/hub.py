"""Fan-out of live messages to every open browser websocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

_QUEUE_LIMIT = 500


class LiveHub:
    """Each subscriber gets its own queue; slow clients drop the oldest message."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self.sent: int = 0

    @property
    def clients(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    def broadcast(self, kind: str, data: dict[str, Any] | None = None) -> None:
        message = json.dumps({"type": kind, "data": data or {}}, separators=(",", ":"))
        self.sent += 1
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(message)
