from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CpuTopology:
    name: str
    physical: int
    logical: int


@dataclass(frozen=True)
class FirewallRule:
    name: str
    port: int
    profile: str
    remote: str
    program: str
    enabled: bool = True


class Backend(Protocol):
    def is_windows(self) -> bool: ...

    def is_admin(self) -> bool: ...

    def relaunch_elevated(self, argv: Sequence[str]) -> int: ...

    def now(self) -> datetime: ...

    def cpu_topology(self) -> CpuTopology: ...

    def lan_address(self) -> str | None: ...

    def network_profile(self) -> str: ...

    def which(self, name: str) -> Path | None: ...

    def find_ansys(self) -> Path | None: ...

    def program_path(self) -> Path: ...

    def fetch(self, url: str, dest: Path) -> None: ...

    def http_get_json(self, url: str, timeout_s: float = 1.0) -> dict | None: ...

    def can_connect(self, host: str, port: int, timeout_s: float = 1.0) -> bool: ...

    def port_owner(self, port: int) -> str | None: ...

    def exists(self, path: Path) -> bool: ...

    def read_bytes(self, path: Path) -> bytes | None: ...

    def write_bytes(self, path: Path, data: bytes) -> None: ...

    def append_text(self, path: Path, text: str) -> None: ...

    def mkdir(self, path: Path) -> None: ...

    def delete(self, path: Path) -> None: ...

    def rmtree(self, path: Path) -> None: ...

    def copy(self, src: Path, dst: Path) -> None: ...

    def unzip(self, archive: Path, dest: Path) -> None: ...

    def restrict_acl(self, path: Path) -> None: ...

    def is_writable(self, path: Path) -> bool: ...

    def stop_process(self, image: str) -> None: ...

    def task_register(
        self,
        name: str,
        program: Path,
        args: str,
        highest: bool,
        restart_minutes: int | None,
    ) -> None: ...

    def task_unregister(self, name: str) -> None: ...

    def task_exists(self, name: str) -> bool: ...

    def task_start(self, name: str) -> None: ...

    def task_stop(self, name: str) -> None: ...

    def task_last_result(self, name: str) -> int | None: ...

    def task_program(self, name: str) -> Path | None: ...

    def fw_get(self, name: str) -> FirewallRule | None: ...

    def fw_set(self, rule: FirewallRule) -> None: ...

    def fw_remove(self, name: str) -> None: ...


MUTATING: frozenset[str] = frozenset(
    {
        "relaunch_elevated",
        "fetch",
        "write_bytes",
        "append_text",
        "mkdir",
        "delete",
        "rmtree",
        "copy",
        "unzip",
        "restrict_acl",
        "stop_process",
        "task_register",
        "task_unregister",
        "task_start",
        "task_stop",
        "fw_set",
        "fw_remove",
    }
)
