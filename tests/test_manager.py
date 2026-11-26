from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from fluentmon.api.bus import Bus
from fluentmon.config import Config
from fluentmon.transcript.manager import (
    SessionChanges,
    SessionEnded,
    SessionManager,
    SessionStarted,
    case_name,
    find_fluent_pid,
)

HEADER = "  iter  continuity  x-velocity  time/iter"


def data_line(it: int, left: int) -> str:
    return f"{it:6d}  1.0e-03  2.0e-03  0:00:10  {left}\n"


def make_config(tmp_path: Path) -> tuple[Config, Path, Path]:
    runs = tmp_path / "runs"
    watch = tmp_path / "watch"
    cfg = Config()
    cfg.server.runs_dir = str(runs)
    cfg.server.watch_dir = str(watch)
    return cfg, runs, watch


def stub(seconds: float) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def write_run(runs: Path, name: str, pid: int | None, console: str = "") -> Path:
    folder = runs / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "console.trn").write_text(console, encoding="utf-8")
    meta = {"case": "C:/cases/duct.cas.h5", "launched": "2026-11-25T10:41:07+09:00", "pid": pid, "cores": 4}
    (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


async def take(queue: asyncio.Queue[object], kind: type, timeout: float = 4.0) -> object:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"no {kind.__name__} within {timeout} s")
        message = await asyncio.wait_for(queue.get(), remaining)
        if isinstance(message, kind):
            return message


def run_with(
    cfg: Config,
    scenario: Callable[[SessionManager, asyncio.Queue[object]], Awaitable[None]],
) -> None:
    async def main() -> None:
        bus = Bus()
        queue = bus.subscribe()
        manager = SessionManager(cfg, bus)
        task = asyncio.create_task(manager.run())
        try:
            await asyncio.sleep(0.05)
            await scenario(manager, queue)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(main())


def test_run_folder_lifecycle(tmp_path: Path) -> None:
    cfg, runs, _ = make_config(tmp_path)
    child = stub(1.5)
    try:
        write_run(runs, "20261125-1041_duct", child.pid, HEADER + "\n" + data_line(1, 99))

        async def scenario(manager: SessionManager, queue: asyncio.Queue[object]) -> None:
            started = await take(queue, SessionStarted)
            assert isinstance(started, SessionStarted)
            assert started.session_id == "20261125-1041_duct"
            assert started.snapshot["session"]["case"] == "duct"
            with (runs / "20261125-1041_duct" / "console.trn").open("a", encoding="utf-8") as handle:
                handle.write(data_line(2, 98) + data_line(3, 97))
            changes = await take(queue, SessionChanges)
            assert isinstance(changes, SessionChanges)
            assert [row[0] for row in changes.changes.rows] == [2, 3]
            ended = await take(queue, SessionEnded, timeout=4.0)
            assert ended.session_id == "20261125-1041_duct"

        run_with(cfg, scenario)
    finally:
        child.kill()
        child.wait()


def test_second_run_ends_first_session(tmp_path: Path) -> None:
    cfg, runs, _ = make_config(tmp_path)
    first_child = stub(10)
    try:
        write_run(runs, "20261125-1041_first", first_child.pid, HEADER + "\n" + data_line(1, 9))

        async def scenario(manager: SessionManager, queue: asyncio.Queue[object]) -> None:
            await take(queue, SessionStarted)
            write_run(runs, "20261125-1100_second", None, HEADER + "\n" + data_line(1, 9))
            ended = await take(queue, SessionEnded)
            assert ended.session_id == "20261125-1041_first"
            started = await take(queue, SessionStarted)
            assert started.session_id == "20261125-1100_second"
            assert manager.sessions()[0].session_id == "20261125-1100_second"

        run_with(cfg, scenario)
    finally:
        first_child.kill()
        first_child.wait()


def test_manual_transcript_attaches(tmp_path: Path) -> None:
    cfg, _, watch = make_config(tmp_path)
    watch.mkdir(parents=True)

    async def scenario(manager: SessionManager, queue: asyncio.Queue[object]) -> None:
        (watch / "replay.trn").write_text(HEADER + "\n" + data_line(1, 9), encoding="utf-8")
        started = await take(queue, SessionStarted)
        assert isinstance(started, SessionStarted)
        assert started.session_id.startswith("replay-")
        assert started.snapshot["session"]["case"] == "replay"

    run_with(cfg, scenario)


def test_large_existing_file_gives_one_started_and_no_changes(tmp_path: Path) -> None:
    cfg, _, watch = make_config(tmp_path)
    watch.mkdir(parents=True)
    body = HEADER + "\n" + "".join(data_line(n, 1000 - n) for n in range(1, 1001))
    (watch / "big.trn").write_text(body, encoding="utf-8")

    async def scenario(manager: SessionManager, queue: asyncio.Queue[object]) -> None:
        started = await take(queue, SessionStarted)
        assert isinstance(started, SessionStarted)
        await asyncio.sleep(0.3)
        leftovers = []
        while not queue.empty():
            leftovers.append(queue.get_nowait())
        assert not [item for item in leftovers if isinstance(item, SessionChanges)]
        assert len([item for item in leftovers if isinstance(item, SessionStarted)]) == 0

    run_with(cfg, scenario)


def test_invalid_session_json_is_ignored_until_valid(tmp_path: Path) -> None:
    cfg, runs, _ = make_config(tmp_path)
    folder = runs / "20261125-1200_half"
    folder.mkdir(parents=True)
    (folder / "console.trn").write_text(HEADER + "\n", encoding="utf-8")
    (folder / "session.json").write_text("{not json", encoding="utf-8")

    async def scenario(manager: SessionManager, queue: asyncio.Queue[object]) -> None:
        await asyncio.sleep(0.2)
        assert manager.current() is None
        meta = {"case": "C:/cases/half.cas.h5", "launched": "2026-11-25T12:00:00+09:00", "pid": None, "cores": 4}
        (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")
        started = await take(queue, SessionStarted)
        assert started.session_id == "20261125-1200_half"

    run_with(cfg, scenario)


def test_check_idle_without_session_is_quiet(tmp_path: Path) -> None:
    cfg, _, _ = make_config(tmp_path)
    manager = SessionManager(cfg, Bus())
    manager.check_idle()
    assert manager.current() is None
    assert manager.sessions() == []


def test_case_name_strips_cas_h5() -> None:
    assert case_name("D:/cfd/duct.cas.h5") == "duct"
    assert case_name("D:/cfd/duct.cas") == "duct"


def test_find_fluent_pid_returns_none_without_fluent() -> None:
    pid = find_fluent_pid()
    assert pid is None or isinstance(pid, int)


def test_transcript_path_for_known_and_unknown_session(tmp_path: Path) -> None:
    cfg, _, watch = make_config(tmp_path)
    watch.mkdir(parents=True)
    transcript = watch / "replay.trn"

    async def scenario(manager: SessionManager, queue: asyncio.Queue[object]) -> None:
        transcript.write_text(HEADER + "\n" + data_line(1, 9), encoding="utf-8")
        started = await take(queue, SessionStarted)
        assert isinstance(started, SessionStarted)
        assert manager.transcript_path(started.session_id) == transcript
        assert manager.transcript_path("no-such-session") is None

    run_with(cfg, scenario)
