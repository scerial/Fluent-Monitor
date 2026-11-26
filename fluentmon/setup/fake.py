from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO

from fluentmon.setup.backend import MUTATING, Backend, CpuTopology, FirewallRule


def _key(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/")


def _under(key: str, root: str) -> bool:
    return key == root or key.startswith(root + "/")


@dataclass
class FakeBackend:
    windows: bool = True
    admin: bool = True
    topology: CpuTopology = CpuTopology("AMD Ryzen 7 7800X3D", 8, 16)
    lan: str | None = "192.168.0.10"
    profile: str = "Private"
    ports: dict[int, str] = field(default_factory=dict)
    reachable: set[tuple[str, int]] = field(default_factory=set)
    json_by_url: dict[str, dict] = field(default_factory=dict)
    downloads: dict[str, bytes] = field(default_factory=dict)
    ansys: Path | None = None
    which_map: dict[str, Path] = field(default_factory=dict)
    program: Path = Path("C:/Users/tester/AppData/Local/uv/tools/fluentmon/Scripts/pythonw.exe")
    now_value: datetime = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    elevated_result: int = 0
    readonly: set[str] = field(default_factory=set)
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    tasks: dict[str, dict] = field(default_factory=dict)
    rules: dict[str, FirewallRule] = field(default_factory=dict)
    calls: list[tuple[str, tuple]] = field(default_factory=list)
    failures: dict[str, list[BaseException]] = field(default_factory=dict)

    def fail_on(self, name: str, exc: BaseException, times: int = 1) -> None:
        self.failures.setdefault(name, []).extend([exc] * times)

    def _call(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)

    def is_windows(self) -> bool:
        self._call("is_windows")
        return self.windows

    def is_admin(self) -> bool:
        self._call("is_admin")
        return self.admin

    def relaunch_elevated(self, argv: Sequence[str]) -> int:
        self._call("relaunch_elevated", tuple(argv))
        return self.elevated_result

    def now(self) -> datetime:
        self._call("now")
        return self.now_value

    def cpu_topology(self) -> CpuTopology:
        self._call("cpu_topology")
        return self.topology

    def lan_address(self) -> str | None:
        self._call("lan_address")
        return self.lan

    def network_profile(self) -> str:
        self._call("network_profile")
        return self.profile

    def which(self, name: str) -> Path | None:
        self._call("which", name)
        return self.which_map.get(name)

    def find_ansys(self) -> Path | None:
        self._call("find_ansys")
        return self.ansys

    def program_path(self) -> Path:
        self._call("program_path")
        return self.program

    def fetch(self, url: str, dest: Path) -> None:
        self._call("fetch", url, dest)
        if url not in self.downloads:
            raise OSError(f"cannot download {url}")
        self.files[_key(dest)] = self.downloads[url]

    def http_get_json(self, url: str, timeout_s: float = 1.0) -> dict | None:
        self._call("http_get_json", url, timeout_s)
        value = self.json_by_url.get(url)
        return None if value is None else dict(value)

    def can_connect(self, host: str, port: int, timeout_s: float = 1.0) -> bool:
        self._call("can_connect", host, port, timeout_s)
        return (host, port) in self.reachable

    def port_owner(self, port: int) -> str | None:
        self._call("port_owner", port)
        return self.ports.get(port)

    def exists(self, path: Path) -> bool:
        self._call("exists", path)
        key = _key(path)
        return key in self.files or key in self.dirs

    def read_bytes(self, path: Path) -> bytes | None:
        self._call("read_bytes", path)
        return self.files.get(_key(path))

    def write_bytes(self, path: Path, data: bytes) -> None:
        self._call("write_bytes", path, data)
        self.files[_key(path)] = bytes(data)

    def append_text(self, path: Path, text: str) -> None:
        self._call("append_text", path, text)
        key = _key(path)
        self.files[key] = self.files.get(key, b"") + text.encode("utf-8")

    def mkdir(self, path: Path) -> None:
        self._call("mkdir", path)
        self.dirs.add(_key(path))

    def delete(self, path: Path) -> None:
        self._call("delete", path)
        self.files.pop(_key(path), None)

    def rmtree(self, path: Path) -> None:
        self._call("rmtree", path)
        root = _key(path)
        for key in [k for k in self.files if _under(k, root)]:
            del self.files[key]
        for key in [k for k in self.dirs if _under(k, root)]:
            self.dirs.discard(key)
        self.dirs.discard(root)

    def copy(self, src: Path, dst: Path) -> None:
        self._call("copy", src, dst)
        source = _key(src)
        if source not in self.files:
            raise FileNotFoundError(str(src))
        self.files[_key(dst)] = self.files[source]

    def unzip(self, archive: Path, dest: Path) -> None:
        self._call("unzip", archive, dest)
        data = self.files.get(_key(archive))
        if data is None:
            raise FileNotFoundError(str(archive))
        root = _key(dest)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise OSError(f"unsafe member in archive: {info.filename}")
                target = f"{root}/{member.as_posix()}"
                if info.is_dir():
                    self.dirs.add(target.rstrip("/"))
                else:
                    self.files[target] = zf.read(info)

    def restrict_acl(self, path: Path) -> None:
        self._call("restrict_acl", path)

    def is_writable(self, path: Path) -> bool:
        self._call("is_writable", path)
        key = _key(path)
        return (key in self.dirs or key in self.files) and key not in self.readonly

    def stop_process(self, image: str) -> None:
        self._call("stop_process", image)

    def task_register(
        self,
        name: str,
        program: Path,
        args: str,
        highest: bool,
        restart_minutes: int | None,
    ) -> None:
        self._call("task_register", name, program, args, highest, restart_minutes)
        self.tasks[name] = {
            "program": str(program),
            "args": args,
            "highest": highest,
            "restart_minutes": restart_minutes,
            "last_result": None,
        }

    def task_unregister(self, name: str) -> None:
        self._call("task_unregister", name)
        self.tasks.pop(name, None)

    def task_exists(self, name: str) -> bool:
        self._call("task_exists", name)
        return name in self.tasks

    def task_start(self, name: str) -> None:
        self._call("task_start", name)
        if name not in self.tasks:
            raise OSError(f"no such task: {name}")
        self.tasks[name]["last_result"] = 0

    def task_stop(self, name: str) -> None:
        self._call("task_stop", name)

    def task_last_result(self, name: str) -> int | None:
        self._call("task_last_result", name)
        task = self.tasks.get(name)
        return None if task is None else task["last_result"]

    def task_program(self, name: str) -> Path | None:
        self._call("task_program", name)
        task = self.tasks.get(name)
        return None if task is None else Path(task["program"])

    def fw_get(self, name: str) -> FirewallRule | None:
        self._call("fw_get", name)
        return self.rules.get(name)

    def fw_set(self, rule: FirewallRule) -> None:
        self._call("fw_set", rule)
        self.rules[rule.name] = replace(rule)

    def fw_remove(self, name: str) -> None:
        self._call("fw_remove", name)
        self.rules.pop(name, None)


class DryRunBackend:
    def __init__(self, inner: Backend, out: IO[str]) -> None:
        self._inner = inner
        self._out = out

    def __getattr__(self, name: str) -> Callable[..., object]:
        if name not in MUTATING:
            return getattr(self._inner, name)

        def would(*args: object) -> object:
            if args:
                self._out.write(f"would {name} {args[0]}\n")
            else:
                self._out.write(f"would {name}\n")
            if name == "relaunch_elevated":
                return 0
            return None

        return would
