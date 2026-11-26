from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

import orjson

from fluentmon.api.hub import Client, client_writer


class _FileSink:
    def __init__(self, handle: IO[bytes], start: float, clock: Callable[[], float]) -> None:
        self._handle = handle
        self._start = start
        self._clock = clock

    async def send_bytes(self, data: bytes) -> None:
        line = orjson.dumps({"at": self._clock() - self._start, "msg": orjson.loads(data)})
        self._handle.write(line + b"\n")
        self._handle.flush()

    async def close(self, code: int = 1000) -> None:
        return None


class RecorderClient:
    def __init__(self, path: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self._handle: IO[bytes] = path.open("wb")
        self.client = Client(_FileSink(self._handle, clock(), clock))

    def start(self) -> asyncio.Task[None]:
        return asyncio.create_task(client_writer(self.client))

    def close(self) -> None:
        self._handle.close()
