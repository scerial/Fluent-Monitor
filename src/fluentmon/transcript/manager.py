from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from fluentmon.api.bus import Bus
from fluentmon.config import Config
from fluentmon.model.session import Changes, SessionState
from fluentmon.transcript.parser import Parser
from fluentmon.transcript.tailer import FileTailer

log = logging.getLogger("fluentmon.transcript.manager")

MANUAL_WINDOW_S = 60.0
MAX_SESSIONS = 20
TRANSCRIPT_SUFFIXES = (".trn", ".log")
CX_NAME = re.compile(r"^cx\d{3,4}\.exe$")


@dataclass
class SessionStarted:
    session_id: str
    snapshot: dict


@dataclass
class SessionChanges:
    session_id: str
    changes: Changes
    bars: dict | None
    snapshot: dict | None


@dataclass
class SessionEnded:
    session_id: str


@dataclass
class Source:
    path: Path
    base_id: str
    case: str
    started: datetime
    pid: int | None
    run_folder: Path | None = None


@dataclass
class Attached:
    state: SessionState
    tailer: FileTailer
    task: asyncio.Task[None]
    exit_task: asyncio.Task[None] | None = None
    source: Source | None = field(default=None)


def find_fluent_pid() -> int | None:
    fallback: int | None = None
    for process in psutil.process_iter(["name", "pid"]):
        name = process.info.get("name") or ""
        if CX_NAME.match(name):
            return int(process.info["pid"])
        if name.lower() == "fluent.exe" and fallback is None:
            fallback = int(process.info["pid"])
    return fallback


def case_name(case: str) -> str:
    name = Path(case).name
    if name.endswith(".cas.h5"):
        return name[: -len(".cas.h5")]
    return Path(name).stem


def read_run_meta(path: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("case"), str):
        return None
    try:
        datetime.fromisoformat(str(meta.get("launched")))
    except ValueError:
        return None
    return meta


class _Handler(FileSystemEventHandler):
    def __init__(self, manager: SessionManager, loop: asyncio.AbstractEventLoop) -> None:
        self._manager = manager
        self._loop = loop

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        dest = getattr(event, "dest_path", "")
        if dest:
            paths.append(dest)
        created = event.event_type in ("created", "moved")
        for raw in paths:
            try:
                self._loop.call_soon_threadsafe(self._manager._on_file, Path(os.fsdecode(raw)), created)
            except RuntimeError:
                log.debug("loop closed, file event dropped")


class SessionManager:
    def __init__(self, cfg: Config, bus: Bus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.runs_dir = Path(cfg.server.runs_dir)
        self.watch_dir = Path(cfg.server.watch_dir)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: list[SessionState] = []
        self._attached: dict[str, Attached] = {}
        self._current: SessionState | None = None
        self._seen_runs: set[str] = set()
        self._seen_files: set[str] = set()
        self._ended_sizes: dict[str, int] = {}

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self._startup_scan()
        observer = Observer()
        handler = _Handler(self, self._loop)
        observer.schedule(handler, str(self.runs_dir), recursive=True)
        observer.schedule(handler, str(self.watch_dir), recursive=False)
        observer.start()
        try:
            await asyncio.Event().wait()
        finally:
            observer.stop()
            observer.join()
            self._shutdown()

    def current(self) -> SessionState | None:
        return self._current

    def sessions(self) -> list[SessionState]:
        return list(reversed(self._sessions[-MAX_SESSIONS:]))

    def transcript_path(self, session_id: str) -> Path | None:
        attached = self._attached.get(session_id)
        if attached is None or attached.source is None:
            return None
        return attached.source.path

    def check_idle(self) -> None:
        if self._current is None:
            return
        changes = self._current.check_idle(time.monotonic())
        if changes.events:
            self.bus.publish(SessionChanges(self._current.session_id, changes, None, None))

    def _shutdown(self) -> None:
        if self._current is not None:
            self._end_session(self._current)
            self._current = None
        for attached in list(self._attached.values()):
            attached.tailer.stop()
            attached.task.cancel()
            if attached.exit_task is not None:
                attached.exit_task.cancel()

    def _startup_scan(self) -> None:
        runs = sorted(
            self.runs_dir.glob("*/session.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for session_json in runs:
            if self._try_run(session_json.parent):
                return
        now = time.time()
        manual = [
            path
            for path in self.watch_dir.iterdir()
            if path.is_file()
            and path.suffix in TRANSCRIPT_SUFFIXES
            and now - path.stat().st_mtime <= MANUAL_WINDOW_S
        ]
        if manual:
            newest = max(manual, key=lambda p: p.stat().st_mtime)
            self._attach_manual(newest)

    def _on_file(self, path: Path, created: bool = False) -> None:
        if path.name == "session.json":
            self._try_run(path.parent)
        elif path.name == "console.trn":
            if (path.parent / "session.json").exists():
                self._try_run(path.parent)
        elif path.parent == self.watch_dir and path.suffix in TRANSCRIPT_SUFFIXES:
            if not path.is_file():
                return
            key = str(path)
            if key not in self._seen_files:
                self._attach_manual(path)
                return
            ended_size = self._ended_sizes.get(key)
            if ended_size is None:
                return
            if not created and path.stat().st_size >= ended_size:
                return
            del self._ended_sizes[key]
            self._attach_manual(path)

    def _try_run(self, folder: Path) -> bool:
        key = str(folder)
        if key in self._seen_runs:
            return False
        meta = read_run_meta(folder / "session.json")
        if meta is None:
            return False
        console = folder / "console.trn"
        if not console.is_file():
            return False
        self._seen_runs.add(key)
        pid = meta.get("pid")
        source = Source(
            path=console,
            base_id=folder.name,
            case=case_name(meta["case"]),
            started=datetime.fromisoformat(str(meta["launched"])),
            pid=int(pid) if isinstance(pid, int) and pid > 0 else None,
            run_folder=folder,
        )
        self._attach(source)
        return True

    def _attach_manual(self, path: Path) -> None:
        self._seen_files.add(str(path))
        started = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        source = Source(
            path=path,
            base_id=f"{path.stem}-{started:%Y%m%d-%H%M}",
            case=path.stem,
            started=started,
            pid=find_fluent_pid(),
        )
        self._attach(source)

    def _unique_id(self, base: str) -> str:
        taken = {state.session_id for state in self._sessions}
        candidate = base
        counter = 2
        while candidate in taken:
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def _attach(self, source: Source) -> None:
        if self._current is not None:
            self._end_session(self._current)
            self._current = None
        assert self._loop is not None
        state = SessionState(self.cfg, self._unique_id(source.base_id), source.case, source.started)
        self._sessions.append(state)
        del self._sessions[:-MAX_SESSIONS]
        self._current = state
        parser = Parser()

        def on_lines(lines: list[str], live: bool) -> None:
            if state.status == "ended":
                return
            changes = Changes()
            now = time.monotonic()
            for line in lines:
                for ev in parser.feed(line):
                    changes.merge(state.apply(ev, now))
            if live:
                snapshot = state.snapshot_msg() if changes.snapshot else None
                self.bus.publish(SessionChanges(state.session_id, changes, state.bars_msg(), snapshot))

        def on_live() -> None:
            if state.status != "ended":
                self.bus.publish(SessionStarted(state.session_id, state.snapshot_msg()))

        def on_restart() -> None:
            if state.status == "ended":
                return
            self._end_session(state)
            if self._current is state:
                self._current = None
            self._attach(source)

        tailer = FileTailer(
            source.path,
            on_lines,
            on_live,
            on_restart,
            self._loop,
            safety_read_s=self.cfg.server.safety_read_s,
        )
        task = self._loop.create_task(tailer.run())
        attached = Attached(state, tailer, task, None, source)
        self._attached[state.session_id] = attached
        if source.pid is not None:
            attached.exit_task = self._loop.create_task(self._watch_exit(state, source.pid))

    async def _watch_exit(self, state: SessionState, pid: int) -> None:
        loop = asyncio.get_running_loop()
        outcome: asyncio.Future[str] = loop.create_future()

        def settle(result: str) -> None:
            if not outcome.done():
                outcome.set_result(result)

        def wait_for_exit() -> None:
            try:
                psutil.Process(pid).wait()
                result = "exited"
            except psutil.NoSuchProcess:
                result = "exited"
            except psutil.Error:
                result = "error"
            try:
                loop.call_soon_threadsafe(settle, result)
            except RuntimeError:
                log.debug("loop closed before process %d was observed", pid)

        threading.Thread(target=wait_for_exit, name=f"fluentmon-exit-{pid}", daemon=True).start()
        result_text = await outcome
        if result_text == "error":
            log.warning("cannot wait for process %d", pid)
            return
        if self._current is state:
            self._end_session(state)
            self._current = None

    def _end_session(self, state: SessionState) -> None:
        if state.status == "ended":
            return
        changes = state.end(time.monotonic())
        self.bus.publish(SessionChanges(state.session_id, changes, state.bars_msg(), None))
        self.bus.publish(SessionEnded(state.session_id))
        attached = self._attached.pop(state.session_id, None)
        if attached is not None:
            if attached.source is not None and attached.source.run_folder is None:
                try:
                    size = attached.source.path.stat().st_size
                except OSError:
                    size = 0
                self._ended_sizes[str(attached.source.path)] = size
            attached.tailer.stop()
            attached.task.cancel()
            if attached.exit_task is not None and attached.exit_task is not asyncio.current_task():
                attached.exit_task.cancel()
