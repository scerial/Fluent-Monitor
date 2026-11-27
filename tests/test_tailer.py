from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fluentmon.transcript.tailer import FileTailer


class Recorder:
    def __init__(self) -> None:
        self.batches: list[tuple[list[str], bool]] = []
        self.live_calls = 0
        self.restarts = 0

    def on_lines(self, lines: list[str], live: bool) -> None:
        self.batches.append((lines, live))

    def on_live(self) -> None:
        self.live_calls += 1

    def on_restart(self) -> None:
        self.restarts += 1

    def all_lines(self) -> list[str]:
        result: list[str] = []
        for lines, _ in self.batches:
            result.extend(lines)
        return result


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def run_scenario(
    path: Path,
    scenario: Callable[[FileTailer, Recorder], Awaitable[None]],
    safety_read_s: float = 0.0,
) -> None:
    async def main() -> None:
        recorder = Recorder()
        loop = asyncio.get_running_loop()
        tailer = FileTailer(
            path,
            recorder.on_lines,
            recorder.on_live,
            recorder.on_restart,
            loop,
            safety_read_s=safety_read_s,
        )
        task = asyncio.create_task(tailer.run())
        try:
            await asyncio.sleep(0.05)
            await scenario(tailer, recorder)
        finally:
            tailer.stop()
            await asyncio.wait_for(task, timeout=3.0)

    asyncio.run(main())


def append(path: Path, text: str) -> None:
    with path.open("ab") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()


def test_ten_lines_in_three_writes_arrive_once_in_order(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_bytes(b"")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        append(path, "line 1\nline 2\nline 3\nline 4")
        await asyncio.sleep(0.05)
        append(path, "\nline 5\nline 6\nline 7\n")
        await asyncio.sleep(0.05)
        append(path, "line 8\nline 9\nline 10\n")
        await wait_until(lambda: len(rec.all_lines()) == 10)
        assert rec.all_lines() == [f"line {n}" for n in range(1, 11)]

    run_scenario(path, scenario)


def test_existing_lines_then_live(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_text("".join(f"old {n}\n" for n in range(100)), encoding="utf-8")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        assert rec.batches[0][1] is False
        assert len(rec.batches[0][0]) == 100
        append(path, "new 1\nnew 2\nnew 3\n")
        await wait_until(lambda: len(rec.all_lines()) == 103)
        assert rec.live_calls == 1
        assert rec.batches[-1][1] is True
        assert rec.all_lines()[-3:] == ["new 1", "new 2", "new 3"]

    run_scenario(path, scenario)


def test_truncate_and_rewrite_calls_restart_once(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_text("".join(f"long line number {n:04d}\n" for n in range(5)), encoding="utf-8")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        path.write_bytes(b"a\nb\n")
        await wait_until(lambda: "b" in rec.all_lines())
        assert rec.restarts == 1
        assert rec.all_lines()[-2:] == ["a", "b"]

    run_scenario(path, scenario)


def test_fifty_rapid_appends_need_fewer_reads(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_bytes(b"")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        counter = {"reads": 0}
        original = tailer._read_chunk

        def counting(handle: Any) -> bytes:
            counter["reads"] += 1
            return original(handle)

        setattr(tailer, "_read_chunk", counting)
        for n in range(50):
            append(path, f"burst {n}\n")
        await wait_until(lambda: len(rec.all_lines()) == 50)
        assert rec.all_lines() == [f"burst {n}" for n in range(50)]
        assert counter["reads"] < 50

    run_scenario(path, scenario)


def test_file_created_after_start_is_attached(tmp_path: Path) -> None:
    path = tmp_path / "late.trn"

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await asyncio.sleep(0.1)
        assert rec.live_calls == 0
        path.write_text("first\nsecond\n", encoding="utf-8")
        await wait_until(lambda: len(rec.all_lines()) == 2)
        assert rec.batches[0][1] is False
        assert rec.live_calls == 1

    run_scenario(path, scenario)


def test_deleted_and_recreated_file_restarts(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_text("one\ntwo\n", encoding="utf-8")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        os.remove(path)
        await asyncio.sleep(0.05)
        path.write_text("fresh\n", encoding="utf-8")
        await wait_until(lambda: "fresh" in rec.all_lines())
        assert rec.restarts == 1

    run_scenario(path, scenario)


def test_other_files_in_folder_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_bytes(b"")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        other = tmp_path / "other.trn"
        other.write_text("not ours\n", encoding="utf-8")
        await asyncio.sleep(0.2)
        assert rec.all_lines() == []

    run_scenario(path, scenario)


def test_empty_batches_are_never_delivered(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_bytes(b"")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        append(path, "partial")
        await asyncio.sleep(0.1)
        append(path, " done\n")
        await wait_until(lambda: rec.all_lines() == ["partial done"])
        assert all(lines for lines, _ in rec.batches)

    run_scenario(path, scenario)


def test_safety_read_keeps_polling_without_change_events(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_bytes(b"")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        setattr(tailer, "_notify", lambda: None)
        await asyncio.sleep(0.3)
        for n in range(1, 4):
            append(path, f"quiet {n}\n")
            await wait_until(lambda: f"quiet {n}" in rec.all_lines())
            await asyncio.sleep(0.3)
        assert rec.all_lines() == ["quiet 1", "quiet 2", "quiet 3"]

    run_scenario(path, scenario, safety_read_s=0.1)


def test_safety_read_runs_without_error(tmp_path: Path) -> None:
    path = tmp_path / "console.trn"
    path.write_bytes(b"")

    async def scenario(tailer: FileTailer, rec: Recorder) -> None:
        await wait_until(lambda: rec.live_calls == 1)
        append(path, "safe\n")
        await wait_until(lambda: rec.all_lines() == ["safe"])

    run_scenario(path, scenario, safety_read_s=0.1)
