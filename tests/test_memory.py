from __future__ import annotations

import os
import subprocess
import sys
import time

import psutil
import pytest

from fluentmon.system.memory import FluentTree, ram_totals


def test_ram_totals_agree_with_psutil() -> None:
    total, used = ram_totals()
    memory = psutil.virtual_memory()
    assert total == memory.total
    assert abs(used - (memory.total - memory.available)) <= 0.05 * memory.total


def test_tree_sizes_a_stub_child_process() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        time.sleep(0.2)
        tree = FluentTree(child.pid)
        assert tree.uss() > 0
    finally:
        child.kill()
        child.wait()


def test_missing_root_gives_zero() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    tree = FluentTree(child.pid)
    assert tree.uss() == 0


def test_no_fluent_processes_gives_zero() -> None:
    tree = FluentTree(None)
    assert tree.uss() == 0


def test_uss_is_cached_until_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    original = psutil.Process.memory_full_info

    def counted(self: psutil.Process) -> object:
        calls.append(1)
        return original(self)

    monkeypatch.setattr(psutil.Process, "memory_full_info", counted)
    clock = {"now": 0.0}
    tree = FluentTree(os.getpid(), refresh_s=1.0, clock=lambda: clock["now"])
    first = tree.uss()
    clock["now"] = 0.5
    second = tree.uss()
    assert first == second
    assert len(calls) == 1
    clock["now"] = 1.5
    tree.uss()
    assert len(calls) == 2


def test_disappearing_process_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        time.sleep(0.2)
        clock = {"now": 0.0}
        tree = FluentTree(child.pid, clock=lambda: clock["now"])
        assert tree.uss() > 0
        child.kill()
        child.wait()
        clock["now"] = 5.0
        assert tree.uss() == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
