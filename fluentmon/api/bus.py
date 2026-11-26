from __future__ import annotations

import asyncio


class Bus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[object]] = set()

    def publish(self, ev: object) -> None:
        for queue in tuple(self._subscribers):
            queue.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue[object]:
        queue: asyncio.Queue[object] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[object]) -> None:
        self._subscribers.discard(queue)
