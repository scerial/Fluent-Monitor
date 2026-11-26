from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from fluentmon.transcript.assembler import LineAssembler

log = logging.getLogger("fluentmon.transcript.tailer")

CHUNK = 1 << 20


def _same_path(candidate: str, target: Path) -> bool:
    return os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(str(target.absolute()))


class _Handler(FileSystemEventHandler):
    def __init__(self, tailer: FileTailer) -> None:
        self._tailer = tailer

    def on_any_event(self, event: FileSystemEvent) -> None:
        paths = [event.src_path]
        dest = getattr(event, "dest_path", "")
        if dest:
            paths.append(dest)
        if any(_same_path(str(p), self._tailer.path) for p in paths):
            self._tailer._notify()


class FileTailer:
    def __init__(
        self,
        path: Path,
        on_lines: Callable[[list[str], bool], None],
        on_live: Callable[[], None],
        on_restart: Callable[[], None],
        loop: asyncio.AbstractEventLoop,
        safety_read_s: float = 0.0,
    ) -> None:
        self.path = path
        self._on_lines = on_lines
        self._on_live = on_live
        self._on_restart = on_restart
        self._loop = loop
        self._safety_read_s = safety_read_s
        self._assembler = LineAssembler()
        self._handle: BinaryIO | None = None
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._live = False
        self._stopped = False
        self._observer: Observer | None = None
        self._drain_requested: asyncio.Event | None = None

    async def run(self) -> None:
        self._drain_requested = asyncio.Event()
        observer = Observer()
        observer.schedule(_Handler(self), str(self.path.parent), recursive=False)
        observer.start()
        self._observer = observer
        try:
            self._drain_once()
            await self._drain_loop()
        finally:
            observer.stop()
            observer.join()
            self._observer = None
            self._close()

    def stop(self) -> None:
        self._stopped = True
        if self._drain_requested is not None:
            try:
                self._loop.call_soon_threadsafe(self._drain_requested.set)
            except RuntimeError:
                log.debug("loop already closed while stopping tailer")

    def _notify(self) -> None:
        if self._drain_requested is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._drain_requested.set)
        except RuntimeError:
            log.debug("loop already closed, notification dropped")

    def _read_chunk(self, handle: BinaryIO) -> bytes:
        return handle.read(CHUNK)

    async def _drain_loop(self) -> None:
        event = self._drain_requested
        assert event is not None
        armed = True
        while not self._stopped:
            if armed and self._safety_read_s > 0:
                try:
                    await asyncio.wait_for(event.wait(), self._safety_read_s)
                except TimeoutError:
                    self._drain_once()
                    armed = False
                    continue
            else:
                await event.wait()
            event.clear()
            if self._stopped:
                return
            self._drain_once()
            armed = True

    def _open(self, identity: tuple[int, int]) -> None:
        self._handle = open(self.path, "rb")
        self._identity = identity
        self._offset = 0
        self._assembler.reset()

    def _close(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None
        self._identity = None

    def _drain_once(self) -> None:
        try:
            status = os.stat(self.path)
        except FileNotFoundError:
            return
        identity = (status.st_dev, status.st_ino)
        if self._handle is None:
            self._open(identity)
        elif identity != self._identity:
            self._close()
            self._open(identity)
            self._on_restart()
        elif status.st_size < self._offset:
            self._offset = 0
            self._assembler.reset()
            assert self._handle is not None
            self._handle.seek(0)
            self._on_restart()
        assert self._handle is not None
        lines: list[str] = []
        while True:
            chunk = self._read_chunk(self._handle)
            if not chunk:
                break
            self._offset += len(chunk)
            lines.extend(self._assembler.feed(chunk))
        if lines:
            self._on_lines(lines, self._live)
        if not self._live:
            self._live = True
            self._on_live()
