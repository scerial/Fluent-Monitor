import hashlib
import io
import zipfile
from pathlib import Path

import orjson
import pytest

import fluentmon.config as config_module
from fluentmon.config import Config
from fluentmon.setup import commands, pins, windows_steps
from fluentmon.setup.fake import FakeBackend
from fluentmon.setup.manifest import Manifest
from fluentmon.setup.runner import StepError
from fluentmon.setup.windows_steps import FinishStep, PreflightStep
from support import make_tree

AMD = "AMD Ryzen 7 7800X3D"
HEALTH_URL = "http://127.0.0.1:8765/api/health"
STATE = Path("C:/ProgramData/FluentMon/setup-state.json")
CONFIG = Path("C:/ProgramData/FluentMon/config.toml")
SENSORS = {
    "Powers": {"Package": "42.1 W"},
    "Clocks": {
        "Core #1 (Effective)": "4500 MHz",
        "Core #3 (Effective)": "4450 MHz",
        "Core #5 (Effective)": "4400 MHz",
        "Core #7 (Effective)": "4600 MHz",
    },
    "Temperatures": {"Core (Tctl/Tdie)": "55.0 °C"},
}
STEP_IDS = [
    "preflight",
    "lhm",
    "lhm_settings",
    "task_lhm",
    "detect",
    "config",
    "firewall",
    "task_app",
    "tailscale",
    "finish",
]
MUTATING_NAMES = {
    "fetch",
    "unzip",
    "stop_process",
    "rmtree",
    "task_register",
    "task_start",
    "task_stop",
    "task_unregister",
    "fw_set",
    "fw_remove",
    "copy",
    "delete",
    "restrict_acl",
    "relaunch_elevated",
}


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def ask_enter(prompt: str) -> str:
    return ""


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeBackend:
    monkeypatch.setenv("ProgramData", "C:/ProgramData")
    backend = FakeBackend()
    archive = make_zip({"LibreHardwareMonitor.exe": b"exe", "LibreHardwareMonitor.dll": b"dll"})
    monkeypatch.setattr(pins, "LHM_URL", "https://example.test/lhm.zip")
    monkeypatch.setattr(pins, "LHM_SHA256", hashlib.sha256(archive).hexdigest())
    backend.downloads[pins.LHM_URL] = archive
    backend.json_by_url[pins.LHM_DATA_URL] = make_tree(AMD, SENSORS)
    backend.json_by_url[HEALTH_URL] = {"ok": True, "version": "0.1.0", "lhm": True}
    backend.reachable.update({("github.com", 443), ("192.168.0.10", 8765)})

    real_load = config_module.load_config

    def loader(path: Path | None = None) -> Config:
        if path is None:
            return Config()
        data = backend.read_bytes(Path(path))
        if data is None:
            return Config()
        target = tmp_path / "config-read.toml"
        target.write_bytes(data)
        return real_load(target)

    monkeypatch.setattr(commands, "load_config", loader)
    monkeypatch.setattr(windows_steps, "load_config", loader)
    return backend


def manifest_ids(backend: FakeBackend) -> list[str]:
    manifest = Manifest(STATE, backend)
    manifest.load()
    return [entry["id"] for entry in manifest.entries()]


def snapshot(backend: FakeBackend) -> tuple:
    return (dict(backend.files), set(backend.dirs), dict(backend.tasks), dict(backend.rules))


def test_setup_yes_installs_everything(machine: FakeBackend) -> None:
    out = io.StringIO()
    assert commands.run_setup(["--yes"], machine, ask_enter, out) == 0
    assert machine.exists(pins.INSTALL_DIR / pins.LHM_EXE)
    assert machine.exists(pins.LHM_SETTINGS)
    assert set(machine.tasks) == {"FluentMon", "FluentMon LHM"}
    assert "FluentMon" in machine.rules
    assert machine.read_bytes(CONFIG) is not None
    assert manifest_ids(machine) == STEP_IDS
    text = out.getvalue()
    assert text.index("pass Config") < text.index("fluentmon://connect?host=")
    assert "token=" in text


def test_second_run_changes_no_task_rule_or_install_file(machine: FakeBackend) -> None:
    assert commands.run_setup(["--yes"], machine, ask_enter, io.StringIO()) == 0
    before_tasks = dict(machine.tasks)
    before_rules = dict(machine.rules)
    before_install = {k for k in machine.files if k.startswith("C:/Program Files")}
    machine.calls.clear()
    assert commands.run_setup(["--yes"], machine, ask_enter, io.StringIO()) == 0
    assert dict(machine.tasks) == before_tasks
    assert dict(machine.rules) == before_rules
    assert {k for k in machine.files if k.startswith("C:/Program Files")} == before_install
    names = {name for name, _ in machine.calls}
    assert not names & {"task_register", "fw_set", "fetch", "unzip", "copy", "task_start"}


def test_dry_run_changes_nothing_and_prints_would_lines(machine: FakeBackend) -> None:
    before = snapshot(machine)
    out = io.StringIO()
    assert commands.run_setup(["--yes", "--dry-run"], machine, ask_enter, out) == 0
    assert snapshot(machine) == before
    text = out.getvalue()
    assert "would detect sensors" in text
    assert "would fetch" in text
    assert "would task_register" in text
    assert "would fw_set" in text
    assert not any(name in MUTATING_NAMES for name, _ in machine.calls)


def test_not_elevated_relaunches_once_and_runs_no_step(machine: FakeBackend) -> None:
    machine.admin = False
    assert commands.run_setup(["--yes"], machine, ask_enter, io.StringIO()) == 0
    relaunches = [args for name, args in machine.calls if name == "relaunch_elevated"]
    assert relaunches == [(("setup", "--yes"),)]
    assert not any(name in MUTATING_NAMES - {"relaunch_elevated"} for name, _ in machine.calls)
    assert not machine.tasks


def test_failure_at_firewall_then_rerun_finishes(machine: FakeBackend) -> None:
    machine.fail_on("fw_set", OSError("rule refused"))
    out = io.StringIO()
    assert commands.run_setup(["--yes"], machine, ask_enter, out) == 1
    assert manifest_ids(machine) == STEP_IDS[:6]
    assert "step firewall failed: rule refused" in out.getvalue()
    assert commands.run_setup(["--yes"], machine, ask_enter, io.StringIO()) == 0
    assert manifest_ids(machine) == STEP_IDS


def test_uninstall_undoes_tasks_rule_and_folder_keeps_config(machine: FakeBackend) -> None:
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    out = io.StringIO()
    assert commands.run_uninstall(["--yes"], machine, ask_enter, out) == 0
    assert machine.tasks == {}
    assert machine.rules == {}
    assert not any(k.startswith("C:/Program Files/FluentMon") for k in machine.files)
    assert machine.read_bytes(CONFIG) is not None
    assert machine.exists(Path("C:/FluentMon/transcripts"))
    again = io.StringIO()
    assert commands.run_uninstall(["--yes"], machine, ask_enter, again) == 0
    assert again.getvalue() == "Nothing to undo.\n"


def test_uninstall_restores_settings_of_a_preexisting_lhm_folder(machine: FakeBackend) -> None:
    original = b'<configuration><appSettings><add key="listenerPort" value="8080"/></appSettings></configuration>'
    machine.mkdir(pins.INSTALL_DIR)
    machine.write_bytes(pins.INSTALL_DIR / pins.LHM_EXE, b"users own install")
    machine.write_bytes(pins.LHM_SETTINGS, original)
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    assert commands.run_uninstall(["--yes"], machine, ask_enter, io.StringIO()) == 0
    assert machine.read_bytes(pins.LHM_SETTINGS) == original
    assert machine.read_bytes(pins.INSTALL_DIR / pins.LHM_EXE) is not None


def test_uninstall_removes_settings_with_a_folder_setup_installed(machine: FakeBackend) -> None:
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    commands.run_uninstall(["--yes"], machine, ask_enter, io.StringIO())
    assert machine.read_bytes(pins.LHM_SETTINGS) is None


def test_uninstall_purge_deletes_config_folder_and_prints_uv_hint(machine: FakeBackend) -> None:
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    out = io.StringIO()
    assert commands.run_uninstall(["--yes", "--purge"], machine, ask_enter, out) == 0
    assert not machine.exists(Path("C:/ProgramData/FluentMon"))
    assert "uv tool uninstall fluentmon" in out.getvalue()


def test_declining_uninstall_changes_nothing(machine: FakeBackend) -> None:
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    before = snapshot(machine)
    out = io.StringIO()
    assert commands.run_uninstall([], machine, lambda prompt: "n", out) == 0
    assert snapshot(machine) == before
    assert "Nothing changed." in out.getvalue()


def test_uninstall_without_manifest_has_nothing_to_undo(machine: FakeBackend) -> None:
    out = io.StringIO()
    assert commands.run_uninstall(["--yes"], machine, ask_enter, out) == 0
    assert out.getvalue() == "Nothing to undo.\n"


def test_doctor_zero_on_finished_and_one_after_rule_removed(machine: FakeBackend) -> None:
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    out = io.StringIO()
    assert commands.run_doctor([], machine, out) == 0
    assert "pass Firewall" in out.getvalue()
    machine.rules.pop("FluentMon")
    assert commands.run_doctor([], machine, io.StringIO()) == 1


def test_doctor_json_parses(machine: FakeBackend) -> None:
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    out = io.StringIO()
    commands.run_doctor(["--json"], machine, out)
    parsed = orjson.loads(out.getvalue())
    assert len(parsed) == 10
    assert all(set(item) == {"name", "level", "detail", "hint"} for item in parsed)


def test_doctor_missing_config_is_a_config_failure(machine: FakeBackend) -> None:
    out = io.StringIO()
    assert commands.run_doctor([], machine, out) == 1
    assert "does not exist" in out.getvalue()


def test_config_folder_created_for_custom_config_path(machine: FakeBackend) -> None:
    custom = Path("D:/new folder/한글/config.toml")
    assert commands.run_setup(["--yes", "--config", str(custom)], machine, ask_enter, io.StringIO()) == 0
    assert machine.read_bytes(custom) is not None


def test_lhm_zip_missing_file_is_usage_error(machine: FakeBackend) -> None:
    out = io.StringIO()
    assert commands.run_setup(["--lhm-zip", "C:/nope/lhm.zip"], machine, ask_enter, out) == 2
    assert "not found" in out.getvalue()


def test_unknown_flag_is_usage_error(machine: FakeBackend) -> None:
    assert commands.run_setup(["--bogus"], machine, ask_enter, io.StringIO()) == 2
    assert commands.run_doctor(["--bogus"], machine, io.StringIO()) == 2
    assert commands.run_uninstall(["--bogus"], machine, ask_enter, io.StringIO()) == 2


def test_build_steps_returns_ten_steps_in_order() -> None:
    steps = commands.build_steps(ask_enter)
    assert [s.id for s in steps] == STEP_IDS


def test_default_config_path_uses_program_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ProgramData", "D:/PD")
    assert commands.default_config_path() == Path("D:/PD") / "FluentMon" / "config.toml"
    monkeypatch.delenv("ProgramData")
    assert commands.default_config_path() == Path("C:\\ProgramData") / "FluentMon" / "config.toml"


def test_preflight_rejects_non_windows() -> None:
    backend = FakeBackend(windows=False)
    backend.reachable.add(("github.com", 443))
    with pytest.raises(StepError, match="Windows 10 or 11"):
        PreflightStep().run(commands_ctx(backend))


def test_preflight_rejects_foreign_port_owner() -> None:
    backend = FakeBackend()
    backend.ports[8765] = "explorer.exe"
    backend.reachable.add(("github.com", 443))
    with pytest.raises(StepError, match="port 8765 is held by explorer.exe"):
        PreflightStep().run(commands_ctx(backend))


def test_preflight_accepts_own_owner_and_skips_github_with_lhm_zip() -> None:
    backend = FakeBackend()
    backend.ports[8085] = "LibreHardwareMonitor.exe"
    backend.ports[8765] = "pythonw.exe"
    assert PreflightStep().run(commands_ctx(backend, lhm_zip=Path("C:/x.zip"))) == {}


def test_preflight_requires_github_without_lhm_zip() -> None:
    with pytest.raises(StepError, match="github.com is not reachable"):
        PreflightStep().run(commands_ctx(FakeBackend()))


def test_finish_retries_server_check_then_fails_with_sleeps(machine: FakeBackend) -> None:
    commands.run_setup(["--yes"], machine, ask_enter, io.StringIO())
    machine.json_by_url[HEALTH_URL] = {"ok": False}
    sleeps: list[float] = []
    with pytest.raises(StepError, match="fmon doctor reports failures"):
        FinishStep(sleep=sleeps.append).run(commands_ctx(machine))
    assert len(sleeps) == 9
    assert all(s == 2.0 for s in sleeps)


def commands_ctx(backend: FakeBackend, lhm_zip: Path | None = None):
    from fluentmon.setup.runner import Context

    manifest = Manifest(STATE, backend)
    manifest.load()
    return Context(
        backend=backend,
        manifest=manifest,
        out=io.StringIO(),
        config_path=CONFIG,
        log_path=Path("C:/ProgramData/FluentMon/logs/setup.log"),
        lhm_zip=lhm_zip,
    )


def test_preflight_accepts_system_on_lhm_port_when_lhm_answers() -> None:
    backend = FakeBackend()
    backend.ports[8085] = "System"
    backend.json_by_url[pins.LHM_DATA_URL] = {"Children": []}
    assert PreflightStep().run(commands_ctx(backend, lhm_zip=Path("C:/x.zip"))) == {}


@pytest.mark.parametrize(("port", "answers"), [(8085, False), (8765, True)])
def test_preflight_rejects_other_system_owners(port: int, answers: bool) -> None:
    backend = FakeBackend()
    backend.ports[port] = "System"
    if answers:
        backend.json_by_url[pins.LHM_DATA_URL] = {"Children": []}
    with pytest.raises(StepError, match=f"port {port} is held by System"):
        PreflightStep().run(commands_ctx(backend, lhm_zip=Path("C:/x.zip")))
