from __future__ import annotations

import base64
import ctypes
import getpass
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import httpx
import psutil

from fluentmon.setup.backend import CpuTopology, FirewallRule

log = logging.getLogger("fluentmon.setup.winbackend")

POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
PREFIX = "$ErrorActionPreference = 'Stop'; "
SUFFIX = """
$__text = (& { __SCRIPT__ } | Out-String)
[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($__text))
"""
TASK_NEVER_RUN = 267011
STOP_NOT_RUNNING = 128
ADMIN_SID = "*S-1-5-32-544"
PRIVATE_PROBE = ("10.255.255.255", 1)

REGISTER_SCRIPT = """
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $env:FMON_PROGRAM -Argument $env:FMON_ARGS
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel $env:FMON_LEVEL
if ($env:FMON_RESTART -eq '') {
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)
} else {
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes ([int]$env:FMON_RESTART))
}
Register-ScheduledTask -TaskName $env:FMON_NAME -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
"""

FIREWALL_GET_SCRIPT = """
$r = Get-NetFirewallRule -DisplayName $env:FMON_NAME -ErrorAction SilentlyContinue | Select-Object -First 1
if ($r) {
    $port = [string](($r | Get-NetFirewallPortFilter).LocalPort)
    $app = [string](($r | Get-NetFirewallApplicationFilter).Program)
    $remote = (($r | Get-NetFirewallAddressFilter).RemoteAddress) -join ','
    [pscustomobject]@{ enabled = ([string]$r.Enabled -eq 'True'); profile = [string]$r.Profile; remote = [string]$remote; port = $port; program = $app } | ConvertTo-Json -Compress
}
"""

FIREWALL_SET_SCRIPT = """
Remove-NetFirewallRule -DisplayName $env:FMON_NAME -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $env:FMON_NAME -Direction Inbound -Action Allow -Protocol TCP -LocalPort $env:FMON_PORT -Profile $env:FMON_PROFILE -RemoteAddress ($env:FMON_REMOTE -split ',') -Program $env:FMON_PROGRAM | Out-Null
if ($env:FMON_ENABLED -eq 'False') { Set-NetFirewallRule -DisplayName $env:FMON_NAME -Enabled False }
"""


def _wrap(script: str) -> str:
    return PREFIX + SUFFIX.replace("__SCRIPT__", script)


def _ps(script: str, **env: str) -> str:
    environment = {**os.environ, **env}
    completed = subprocess.run(
        POWERSHELL + [_wrap(script)],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise OSError(detail or f"PowerShell exited with {completed.returncode}")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return ""
    try:
        return base64.b64decode(lines[-1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise OSError(f"PowerShell returned text that is not base64 UTF-8: {exc}") from exc


class WindowsBackend:
    def is_windows(self) -> bool:
        return sys.platform == "win32"

    def is_admin(self) -> bool:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def relaunch_elevated(self, argv: Sequence[str]) -> int:
        fmon = Path(sys.executable).with_name("fmon.exe")
        script = (
            "$p = Start-Process -FilePath $env:FMON_EXE -ArgumentList $env:FMON_ARGS "
            "-Verb RunAs -Wait -PassThru; Write-Output $p.ExitCode"
        )
        try:
            output = _ps(script, FMON_EXE=str(fmon), FMON_ARGS=subprocess.list2cmdline(list(argv)))
        except OSError as exc:
            log.error("elevation was refused or failed: %s", exc)
            return 2
        try:
            return int(output.strip())
        except ValueError:
            return 2

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def cpu_topology(self) -> CpuTopology:
        import winreg

        physical = psutil.cpu_count(logical=False) or 1
        logical = psutil.cpu_count(logical=True) or physical
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return CpuTopology(str(name).strip(), physical, logical)

    def lan_address(self) -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(PRIVATE_PROBE)
                return str(probe.getsockname()[0])
        except OSError:
            return None

    def network_profile(self) -> str:
        address = self.lan_address()
        if address is None:
            return "Unknown"
        script = (
            "$i = Get-NetIPAddress -IPAddress $env:FMON_ADDR -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($i) { (Get-NetConnectionProfile -InterfaceIndex $i.InterfaceIndex).NetworkCategory } else { 'Unknown' }"
        )
        try:
            category = _ps(script, FMON_ADDR=address).strip()
        except OSError:
            return "Unknown"
        return category or "Unknown"

    def which(self, name: str) -> Path | None:
        found = shutil.which(name)
        return Path(found) if found else None

    def find_ansys(self) -> Path | None:
        configured = os.environ.get("ANSYS_INC")
        if configured and Path(configured).is_dir():
            return Path(configured)
        default = Path("C:/Program Files/ANSYS Inc")
        return default if default.is_dir() else None

    def program_path(self) -> Path:
        return Path(sys.executable).with_name("pythonw.exe")

    def fetch(self, url: str, dest: Path) -> None:
        if not url.startswith("https://"):
            raise OSError(f"only HTTPS downloads are allowed: {url}")
        partial = dest.with_name(dest.name + ".part")
        try:
            with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            os.replace(partial, dest)
        except (httpx.HTTPError, OSError) as exc:
            partial.unlink(missing_ok=True)
            raise OSError(f"cannot download {url}: {exc}") from exc

    def http_get_json(self, url: str, timeout_s: float = 1.0) -> dict | None:
        try:
            response = httpx.get(url, timeout=timeout_s)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def can_connect(self, host: str, port: int, timeout_s: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError:
            return False

    def port_owner(self, port: int) -> str | None:
        try:
            connections = psutil.net_connections(kind="tcp")
        except psutil.Error:
            return "unknown"
        for connection in connections:
            if connection.laddr and connection.laddr.port == port and connection.status == psutil.CONN_LISTEN:
                if connection.pid is None:
                    return "unknown"
                try:
                    return psutil.Process(connection.pid).name()
                except psutil.Error:
                    return "unknown"
        return None

    def exists(self, path: Path) -> bool:
        return path.exists()

    def read_bytes(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def write_bytes(self, path: Path, data: bytes) -> None:
        descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def append_text(self, path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def rmtree(self, path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)

    def copy(self, src: Path, dst: Path) -> None:
        shutil.copy2(src, dst)

    def unzip(self, archive: Path, dest: Path) -> None:
        root = dest.resolve()
        with zipfile.ZipFile(archive) as zf:
            planned = []
            for info in zf.infolist():
                target = (root / info.filename).resolve()
                if target != root and root not in target.parents:
                    raise OSError(f"archive member leaves the target folder: {info.filename}")
                planned.append((info, target))
            for info, target in planned:
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)

    def restrict_acl(self, path: Path) -> None:
        user = os.environ.get("USERNAME") or getpass.getuser()
        completed = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)", "/grant:r", f"{ADMIN_SID}:(F)"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.strip() or f"icacls failed on {path}")

    def is_writable(self, path: Path) -> bool:
        if not path.exists():
            return False
        if path.is_dir():
            try:
                with tempfile.TemporaryFile(dir=path):
                    return True
            except OSError:
                return False
        return os.access(path, os.W_OK)

    def stop_process(self, image: str) -> None:
        completed = subprocess.run(["taskkill", "/F", "/IM", image], capture_output=True, text=True, check=False)
        if completed.returncode not in (0, STOP_NOT_RUNNING):
            raise OSError(completed.stderr.strip() or f"taskkill failed for {image}")

    def task_register(
        self,
        name: str,
        program: Path,
        args: str,
        highest: bool,
        restart_minutes: int | None,
    ) -> None:
        _ps(
            REGISTER_SCRIPT,
            FMON_NAME=name,
            FMON_PROGRAM=str(program),
            FMON_ARGS=args,
            FMON_LEVEL="Highest" if highest else "Limited",
            FMON_RESTART="" if restart_minutes is None else str(restart_minutes),
        )

    def task_unregister(self, name: str) -> None:
        _ps(
            "Unregister-ScheduledTask -TaskName $env:FMON_NAME -Confirm:$false -ErrorAction SilentlyContinue",
            FMON_NAME=name,
        )

    def task_exists(self, name: str) -> bool:
        output = _ps(
            "if (Get-ScheduledTask -TaskName $env:FMON_NAME -ErrorAction SilentlyContinue) "
            "{ Write-Output 1 } else { Write-Output 0 }",
            FMON_NAME=name,
        )
        return output.strip() == "1"

    def task_start(self, name: str) -> None:
        _ps("Start-ScheduledTask -TaskName $env:FMON_NAME", FMON_NAME=name)

    def task_stop(self, name: str) -> None:
        _ps("Stop-ScheduledTask -TaskName $env:FMON_NAME -ErrorAction SilentlyContinue", FMON_NAME=name)

    def task_last_result(self, name: str) -> int | None:
        output = _ps(
            "$i = Get-ScheduledTaskInfo -TaskName $env:FMON_NAME -ErrorAction SilentlyContinue; "
            "if ($i) { Write-Output $i.LastTaskResult } else { Write-Output 'missing' }",
            FMON_NAME=name,
        ).strip()
        if output in ("", "missing"):
            return None
        try:
            value = int(output)
        except ValueError:
            return None
        return None if value == TASK_NEVER_RUN else value

    def task_program(self, name: str) -> Path | None:
        output = _ps(
            "$t = Get-ScheduledTask -TaskName $env:FMON_NAME -ErrorAction SilentlyContinue; "
            "if ($t) { Write-Output $t.Actions[0].Execute }",
            FMON_NAME=name,
        ).strip()
        return Path(output) if output else None

    def fw_get(self, name: str) -> FirewallRule | None:
        output = _ps(FIREWALL_GET_SCRIPT, FMON_NAME=name).strip()
        if not output:
            return None
        data = json.loads(output)
        port = data["port"]
        return FirewallRule(
            name=name,
            port=int(port) if port.isdigit() else 0,
            profile=data["profile"],
            remote=data["remote"],
            program=data["program"],
            enabled=bool(data["enabled"]),
        )

    def fw_set(self, rule: FirewallRule) -> None:
        _ps(
            FIREWALL_SET_SCRIPT,
            FMON_NAME=rule.name,
            FMON_PORT=str(rule.port),
            FMON_PROFILE=rule.profile,
            FMON_REMOTE=rule.remote,
            FMON_PROGRAM=rule.program,
            FMON_ENABLED="True" if rule.enabled else "False",
        )

    def fw_remove(self, name: str) -> None:
        _ps("Remove-NetFirewallRule -DisplayName $env:FMON_NAME -ErrorAction SilentlyContinue", FMON_NAME=name)
