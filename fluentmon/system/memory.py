from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import psutil

log = logging.getLogger("fluentmon.system.memory")

FLUENT_NAME = re.compile(r"^(fluent|cx\d{3,4}|fl\d{3,4}|fl_mpi\d{3,4})\.exe$")
HELPER_NAMES = frozenset({"hydra_pmi_proxy.exe", "mpiexec.exe"})


def ram_totals() -> tuple[int, int]:
    memory = psutil.virtual_memory()
    return memory.total, memory.total - memory.available


def _process_size(process: psutil.Process) -> int:
    try:
        return process.memory_full_info().uss
    except psutil.AccessDenied:
        return process.memory_info().private


class FluentTree:
    def __init__(
        self,
        root_pid: int | None,
        refresh_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root_pid = root_pid
        self.refresh_s = refresh_s
        self.clock = clock
        self._processes: list[psutil.Process] | None = None
        self._cached = 0
        self._cached_at: float | None = None

    def uss(self) -> int:
        now = self.clock()
        if self._cached_at is not None and now - self._cached_at < self.refresh_s:
            return self._cached
        total = self._sum()
        self._cached = total
        self._cached_at = now
        return total

    def _sum(self) -> int:
        if self._processes is None:
            self._processes = self._resolve()
        total = 0
        stale = False
        for process in self._processes:
            try:
                total += _process_size(process)
            except psutil.NoSuchProcess:
                stale = True
        if stale:
            self._processes = self._resolve()
            total = 0
            for process in self._processes:
                try:
                    total += _process_size(process)
                except psutil.NoSuchProcess:
                    continue
        return total

    def _resolve(self) -> list[psutil.Process]:
        if self.root_pid is not None:
            try:
                root = psutil.Process(self.root_pid)
                return [root, *root.children(recursive=True)]
            except psutil.NoSuchProcess:
                return []
        roots: list[psutil.Process] = []
        for process in psutil.process_iter(["name"]):
            name = process.info.get("name") or ""
            if FLUENT_NAME.match(name):
                roots.append(process)
        found: list[psutil.Process] = list(roots)
        for root in roots:
            try:
                for child in root.children(recursive=True):
                    if (child.name() or "") in HELPER_NAMES:
                        found.append(child)
            except psutil.NoSuchProcess:
                continue
        return found
