from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import orjson

log = logging.getLogger("fluentmon.api.hub")

QUEUE_LIMIT = 1000
CLOSE_SLOW = 4408


@dataclass(eq=False)
class Client:
    ws: Any
    queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_LIMIT))
    console: bool = True
    closed: bool = False

    def close(self, code: int) -> None:
        if self.closed:
            return
        self.closed = True
        asyncio.ensure_future(self.ws.close(code=code))


class Hub:
    def __init__(self, poller: PcPoller, idle_s: float = 5.0) -> None:
        self.clients: set[Client] = set()
        self.poller = poller
        self.idle: asyncio.TimerHandle | None = None
        self.idle_s = idle_s
        self.on_idle: Callable[[], None] | None = None

    def start(self) -> None:
        self.rearm_idle()

    def send(self, msg: dict) -> None:
        data = orjson.dumps(msg)
        is_console = msg["t"] == "con"
        for client in tuple(self.clients):
            if is_console and not client.console:
                continue
            try:
                client.queue.put_nowait(data)
            except asyncio.QueueFull:
                client.close(code=CLOSE_SLOW)

    def send_to(self, client: Client, msg: dict) -> None:
        data = orjson.dumps(msg)
        try:
            client.queue.put_nowait(data)
        except asyncio.QueueFull:
            client.close(code=CLOSE_SLOW)

    def broadcast(self, msg: dict) -> None:
        self.send(msg)
        self.poller.request()
        self.rearm_idle()

    def rearm_idle(self) -> None:
        if self.idle:
            self.idle.cancel()
        self.idle = asyncio.get_running_loop().call_later(self.idle_s, self.idle_poll)

    def idle_poll(self) -> None:
        if self.on_idle is not None:
            self.on_idle()
        self.poller.request()
        self.rearm_idle()


class PcPoller:
    def __init__(self, send: Callable[[dict], None], sample: Callable[[], dict], min_gap_s: float = 0.2) -> None:
        self.send = send
        self.sample = sample
        self.min_gap_s = min_gap_s
        self.running = False
        self.dirty = False
        self.last_at = 0.0
        self.task: asyncio.Task[None] | None = None

    def request(self) -> None:
        if self.running:
            self.dirty = True
            return
        self.running = True
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        try:
            while True:
                wait = self.last_at + self.min_gap_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.dirty = False
                try:
                    msg = await asyncio.to_thread(self.sample)
                except Exception:
                    log.exception("PC status poll failed")
                else:
                    self.last_at = time.monotonic()
                    self.send(msg)
                if not self.dirty:
                    return
        finally:
            self.running = False


async def client_writer(c: Client) -> None:
    while True:
        data = await c.queue.get()
        await c.ws.send_bytes(data)
