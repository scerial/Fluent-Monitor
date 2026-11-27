import base64
import json
import os
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from fluentmon.setup import winbackend
from fluentmon.setup.backend import MUTATING, Backend, FirewallRule
from fluentmon.setup.winbackend import WindowsBackend

INSTALL_PS1 = Path(__file__).resolve().parent.parent / "installer" / "install.ps1"
EVIL = "a path 'quoted' \"double\" $dollar \u0431\u0443\u043a\u0432\u044b"


@pytest.fixture(autouse=True)
def windows_only_tests(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("windows") and sys.platform != "win32":
        pytest.skip("needs a real Windows PC")


class Recorder:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii") + "\n"


KOREAN_PATH = "C:/Users/사용자/바탕 화면/FluentMon 설정"


def protocol_names() -> list[str]:
    return [n for n in vars(Backend) if not n.startswith("_")]


def test_class_satisfies_backend_protocol() -> None:
    for name in protocol_names():
        assert callable(getattr(WindowsBackend, name)), name
    assert MUTATING <= set(protocol_names())


def test_unzip_extracts_a_normal_archive(tmp_path: Path) -> None:
    archive = tmp_path / "lhm.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("LibreHardwareMonitor.exe", b"exe")
        zf.writestr("sub/a.dll", b"dll")
    dest = tmp_path / "한글 folder" / "lhm"
    WindowsBackend().unzip(archive, dest)
    assert (dest / "LibreHardwareMonitor.exe").read_bytes() == b"exe"
    assert (dest / "sub" / "a.dll").read_bytes() == b"dll"


def test_unzip_rejects_zip_slip(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", b"x")
    dest = tmp_path / "lhm"
    with pytest.raises(OSError, match="leaves the target folder"):
        WindowsBackend().unzip(archive, dest)
    assert not (tmp_path / "escape.txt").exists()


def test_write_bytes_round_trip_with_spaces_and_korean(tmp_path: Path) -> None:
    backend = WindowsBackend()
    target = tmp_path / EVIL.replace("$", "_").replace('"', "_").replace("'", "_")
    target.mkdir()
    path = target / "설정 file.toml"
    backend.write_bytes(path, b"payload \xff\x00")
    assert backend.read_bytes(path) == b"payload \xff\x00"
    assert sorted(p.name for p in target.iterdir()) == ["설정 file.toml"]


def test_write_bytes_interrupted_leaves_no_temp_and_no_half_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = WindowsBackend()
    target = tmp_path / "config.toml"
    backend.write_bytes(target, b"old")

    def broken_replace(src, dst):
        raise OSError("interrupted")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="interrupted"):
        backend.write_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_ps_puts_every_value_in_environment_never_in_script(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(stdout=b64("ok"))
    monkeypatch.setattr(subprocess, "run", recorder)
    output = winbackend._ps("Write-Output $env:FMON_NAME", FMON_NAME=EVIL, FMON_ARGS="-m fluentmon")
    assert output == "ok"
    args, kwargs = recorder.calls[0]
    assert args[:6] == winbackend.POWERSHELL
    assert EVIL not in args[-1]
    assert kwargs["env"]["FMON_NAME"] == EVIL
    assert kwargs["env"]["FMON_ARGS"] == "-m fluentmon"


def test_ps_raises_oserror_with_error_text_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(returncode=1, stderr="access denied"))
    with pytest.raises(OSError, match="access denied"):
        winbackend._ps("Get-Thing")


def test_ps_warnings_on_stderr_with_exit_zero_are_not_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(returncode=0, stdout=b64("value"), stderr="WARNING: x"))
    assert winbackend._ps("Write-Output 1") == "value"


def test_ps_returns_korean_text_through_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(stdout=b64(KOREAN_PATH)))
    assert winbackend._ps("Write-Output $env:FMON_NAME", FMON_NAME="x") == KOREAN_PATH


def test_ps_rejects_output_that_is_not_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(stdout="plain text, not base64!\n"))
    with pytest.raises(OSError, match="not base64 UTF-8"):
        winbackend._ps("Write-Output 1")


def test_ps_empty_output_is_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(stdout=""))
    assert winbackend._ps("Write-Output 1") == ""


def test_ps_script_is_wrapped_and_values_stay_out_of_it(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(stdout=b64("1"))
    monkeypatch.setattr(subprocess, "run", recorder)
    winbackend._ps("Write-Output 1", FMON_NAME=KOREAN_PATH)
    args, kwargs = recorder.calls[0]
    assert "ToBase64String" in args[-1]
    assert KOREAN_PATH not in args[-1]
    assert kwargs["env"]["FMON_NAME"] == KOREAN_PATH


def test_task_program_with_korean_path_through_fake_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(stdout=b64(KOREAN_PATH + "\\pythonw.exe")))
    assert WindowsBackend().task_program("FluentMon") == Path(KOREAN_PATH + "\\pythonw.exe")


def test_fw_get_with_korean_program_through_fake_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"enabled": True, "profile": "Private", "remote": "LocalSubnet", "port": "8765", "program": KOREAN_PATH}
    monkeypatch.setattr(subprocess, "run", Recorder(stdout=b64(json.dumps(payload, ensure_ascii=False))))
    rule = WindowsBackend().fw_get("FluentMon")
    assert rule is not None and rule.program == KOREAN_PATH


def test_fw_get_empty_output_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(stdout=""))
    assert WindowsBackend().fw_get("FluentMon") is None


def test_task_register_passes_values_through_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: captured.append((script, env)) or "")
    backend = WindowsBackend()
    backend.task_register("FluentMon", Path(f"C:/{EVIL}/pythonw.exe"), "-m fluentmon", True, None)
    script, env = captured[0]
    assert EVIL not in script
    assert "FluentMon" not in script
    assert env["FMON_NAME"] == "FluentMon"
    assert env["FMON_PROGRAM"] == f"C:/{EVIL}/pythonw.exe"
    assert env["FMON_ARGS"] == "-m fluentmon"
    assert env["FMON_LEVEL"] == "Highest"
    assert env["FMON_RESTART"] == ""
    assert "RestartCount 999" in script


def test_register_script_omits_empty_argument() -> None:
    script = winbackend.REGISTER_SCRIPT
    assert "[string]::IsNullOrEmpty($env:FMON_ARGS)" in script
    assert "New-ScheduledTaskAction -Execute $env:FMON_PROGRAM\n" in script
    assert "[string]::IsNullOrEmpty($env:FMON_RESTART)" in script
    assert "-eq ''" not in script


def test_task_register_with_no_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: captured.append(env) or "")
    WindowsBackend().task_register("FluentMon LHM", Path("C:/LHM/LibreHardwareMonitor.exe"), "", True, 1)
    assert captured[0]["FMON_ARGS"] == ""
    assert captured[0]["FMON_RESTART"] == "1"


def test_task_last_result_never_run_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = WindowsBackend()
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "267011\n")
    assert backend.task_last_result("FluentMon") is None
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "0\n")
    assert backend.task_last_result("FluentMon") == 0
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "missing\n")
    assert backend.task_last_result("FluentMon") is None


def test_task_program_reads_first_action(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = WindowsBackend()
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "C:\\Users\\tester\\pythonw.exe\n")
    assert backend.task_program("FluentMon") == Path("C:\\Users\\tester\\pythonw.exe")
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "")
    assert backend.task_program("FluentMon") is None


def test_task_exists_reads_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = WindowsBackend()
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "1\n")
    assert backend.task_exists("FluentMon") is True
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "0\n")
    assert backend.task_exists("FluentMon") is False


def test_fw_get_parses_json_and_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = WindowsBackend()
    payload = {"enabled": True, "profile": "Private", "remote": "LocalSubnet", "port": "8765", "program": "C:/x/pythonw.exe"}
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: json.dumps(payload))
    assert backend.fw_get("FluentMon") == FirewallRule("FluentMon", 8765, "Private", "LocalSubnet", "C:/x/pythonw.exe", True)
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "")
    assert backend.fw_get("FluentMon") is None


def test_fw_set_passes_values_through_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: captured.append((script, env)) or "")
    rule = FirewallRule("FluentMon", 8765, "Private", "LocalSubnet", f"C:/{EVIL}/pythonw.exe", False)
    WindowsBackend().fw_set(rule)
    script, env = captured[0]
    assert EVIL not in script
    assert env["FMON_PROGRAM"] == f"C:/{EVIL}/pythonw.exe"
    assert env["FMON_PORT"] == "8765"
    assert env["FMON_ENABLED"] == "False"
    assert "Remove-NetFirewallRule" in script


def test_stop_process_accepts_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Recorder(returncode=128))
    WindowsBackend().stop_process("LibreHardwareMonitor.exe")
    monkeypatch.setattr(subprocess, "run", Recorder(returncode=1, stderr="denied"))
    with pytest.raises(OSError, match="denied"):
        WindowsBackend().stop_process("LibreHardwareMonitor.exe")


def test_restrict_acl_grants_only_user_and_administrators(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorder = Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setenv("USERNAME", "tester")
    WindowsBackend().restrict_acl(tmp_path / "config.toml")
    args, _ = recorder.calls[0]
    assert args[0] == "icacls"
    assert "/inheritance:r" in args
    assert "tester:(F)" in args
    assert "*S-1-5-32-544:(F)" in args


def test_fetch_refuses_plain_http_and_names_url_when_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(OSError, match="only HTTPS"):
        WindowsBackend().fetch("http://example.test/x.zip", tmp_path / "x.zip")

    def offline(*args, **kwargs):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "stream", offline)
    with pytest.raises(OSError, match="https://example.test/x.zip"):
        WindowsBackend().fetch("https://example.test/x.zip", tmp_path / "x.zip")
    assert not (tmp_path / "x.zip.part").exists()


def test_http_get_json_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", offline)
    assert WindowsBackend().http_get_json("http://127.0.0.1:8085/data.json") is None


def test_can_connect_is_false_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", refused)
    assert WindowsBackend().can_connect("github.com", 443) is False


def test_relaunch_returns_elevated_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "0\n")
    assert WindowsBackend().relaunch_elevated(["setup", "--yes"]) == 0
    monkeypatch.setattr(winbackend, "_ps", lambda script, **env: "3\n")
    assert WindowsBackend().relaunch_elevated(["setup"]) == 3

    def refused(script, **env):
        raise OSError("The operation was canceled by the user")

    monkeypatch.setattr(winbackend, "_ps", refused)
    assert WindowsBackend().relaunch_elevated(["setup"]) == 2


def test_find_ansys_prefers_environment_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = tmp_path / "ANSYS v241"
    folder.mkdir()
    monkeypatch.setenv("ANSYS_INC", str(folder))
    assert WindowsBackend().find_ansys() == folder
    monkeypatch.setenv("ANSYS_INC", str(tmp_path / "missing"))
    assert WindowsBackend().find_ansys() is None


def test_program_path_is_pythonw_next_to_interpreter() -> None:
    assert WindowsBackend().program_path() == Path(sys.executable).with_name("pythonw.exe")


def test_is_windows_is_false_here() -> None:
    assert WindowsBackend().is_windows() is (sys.platform == "win32")


def test_read_bytes_of_missing_file_is_none(tmp_path: Path) -> None:
    assert WindowsBackend().read_bytes(tmp_path / "missing.json") is None


def test_lan_address_is_string_or_none() -> None:
    address = WindowsBackend().lan_address()
    assert address is None or isinstance(address, str)


def test_now_is_timezone_aware() -> None:
    assert WindowsBackend().now().tzinfo is not None


def test_install_script_has_expected_structure() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "$ErrorActionPreference = 'Stop'" in text
    assert "git+https://github.com/scerial/Fluent-Monitor" in text
    assert "uv tool install --python 3.12 --force" in text
    assert "uv tool update-shell" in text
    assert "fmon.exe" in text
    assert "setup @Rest" in text
    assert "Get-Command uv" in text
    for line in text.splitlines():
        assert not line.lstrip().startswith("#")


@pytest.mark.windows
def test_windows_read_only_methods_return_sane_values() -> None:
    backend = WindowsBackend()
    topology = backend.cpu_topology()
    assert topology.name
    assert topology.physical > 0 and topology.logical > 0
    assert backend.program_path().exists()
    assert backend.port_owner(1) is None
    assert backend.is_writable(Path(os.environ.get("TEMP", "C:/Windows/Temp"))) is True


@pytest.mark.windows
def test_windows_task_round_trip_when_elevated(tmp_path: Path) -> None:
    backend = WindowsBackend()
    if not backend.is_admin():
        pytest.skip("needs an elevated shell")
    name = "FluentMonTest"
    try:
        backend.task_register(name, backend.program_path(), "-c pass", False, 1)
        assert backend.task_exists(name)
        assert backend.task_program(name) == backend.program_path()
        backend.task_register(name, backend.program_path(), "-c pass", True, 1)
        assert backend.task_exists(name)
    finally:
        backend.task_unregister(name)
    assert not backend.task_exists(name)


@pytest.mark.windows
def test_windows_firewall_round_trip_when_elevated() -> None:
    backend = WindowsBackend()
    if not backend.is_admin():
        pytest.skip("needs an elevated shell")
    name = "FluentMonTest"
    rule = FirewallRule(name, 8799, "Private", "LocalSubnet", str(backend.program_path()), True)
    try:
        backend.fw_set(rule)
        read = backend.fw_get(name)
        assert read is not None
        assert read.port == 8799
        assert read.profile == "Private"
        backend.fw_set(FirewallRule(name, 8799, "Private", "LocalSubnet,100.64.0.0/10", str(backend.program_path())))
        assert "100.64.0.0/10" in backend.fw_get(name).remote
    finally:
        backend.fw_remove(name)
    assert backend.fw_get(name) is None


@pytest.mark.windows
def test_windows_install_script_parses() -> None:
    script = (
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile($env:FMON_PATH, [ref]$null, [ref]$errors) | Out-Null; "
        "Write-Output $errors.Count"
    )
    assert _ps_count(script, str(INSTALL_PS1)) == "0"


def _ps_count(script: str, path: str) -> str:
    return winbackend._ps(script, FMON_PATH=path).strip()
