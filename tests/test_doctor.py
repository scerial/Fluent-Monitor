import io
from collections.abc import Callable
from pathlib import Path

import orjson
import pytest

from fluentmon.config import Config
from fluentmon.doctor import Check, exit_code, render, run_checks
from fluentmon.setup.backend import FirewallRule
from fluentmon.setup.fake import FakeBackend
from fluentmon.setup.pins import LHM_DATA_URL
from support import make_tree

AMD = "AMD Ryzen 7 7800X3D"
HEALTH_URL = "http://127.0.0.1:8765/api/health"
NAMES = ["LHM", "Sensors", "Server", "Port", "Network profile", "Firewall", "Tasks", "Folders", "Config", "Fluent"]
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


def finished() -> tuple[FakeBackend, Config]:
    backend = FakeBackend()
    backend.json_by_url[LHM_DATA_URL] = make_tree(AMD, SENSORS)
    backend.json_by_url[HEALTH_URL] = {"ok": True, "version": "0.1.0", "session": None, "lhm": True}
    backend.fw_set(FirewallRule("FluentMon", 8765, "Private", "LocalSubnet", str(backend.program_path())))
    for name in ("FluentMon", "FluentMon LHM"):
        backend.task_register(name, backend.program_path(), "", False, 1)
        backend.task_start(name)
    backend.mkdir(Path("C:/FluentMon/transcripts"))
    backend.mkdir(Path("C:/FluentMon/runs"))
    backend.reachable.add(("192.168.0.10", 8765))
    backend.ansys = Path("C:/Program Files/ANSYS Inc/v241")
    cfg = Config()
    cfg.server.token = "t" * 32
    return backend, cfg


def levels(checks: list[Check]) -> dict[str, str]:
    return {c.name: c.level for c in checks}


def test_all_pass_on_finished_setup() -> None:
    backend, cfg = finished()
    checks = run_checks(cfg, backend)
    assert [c.name for c in checks] == NAMES
    assert all(c.level == "pass" for c in checks)
    assert exit_code(checks) == 0


def test_all_pass_render_text_is_stable() -> None:
    backend, cfg = finished()
    out = io.StringIO()
    render(run_checks(cfg, backend), out, False)
    assert out.getvalue() == (
        "pass LHM              http://127.0.0.1:8085/data.json answered\n"
        "pass Sensors          AMD Ryzen 7 7800X3D/Powers/Package=42.1, "
        "AMD Ryzen 7 7800X3D/Temperatures/Core (Tctl/Tdie)=55, "
        "AMD Ryzen 7 7800X3D/Clocks/Core #1 (Effective)=4500, "
        "AMD Ryzen 7 7800X3D/Clocks/Core #3 (Effective)=4450, "
        "AMD Ryzen 7 7800X3D/Clocks/Core #5 (Effective)=4400, "
        "AMD Ryzen 7 7800X3D/Clocks/Core #7 (Effective)=4600\n"
        "pass Server           health ok, LHM connected\n"
        "pass Port             192.168.0.10:8765 accepts connections\n"
        "pass Network profile  Private\n"
        "pass Firewall         rule FluentMon is enabled on Private for port 8765\n"
        "pass Tasks            FluentMon and FluentMon LHM are registered, running or finished with result 0\n"
        "pass Folders          C:/FluentMon/transcripts and C:/FluentMon/runs are writable\n"
        "pass Config           config parses; token has 32 characters or more\n"
        "pass Fluent           ANSYS install found at C:/Program Files/ANSYS Inc/v241\n"
    )


@pytest.mark.parametrize(("result", "level"), [(267009, "pass"), (0, "pass"), (1, "fail"), (267014, "fail")])
def test_task_result_levels(result: int, level: str) -> None:
    backend, cfg = finished()
    for name in ("FluentMon", "FluentMon LHM"):
        backend.tasks[name]["last_result"] = result
    assert levels(run_checks(cfg, backend))["Tasks"] == level


def test_json_form_has_ten_objects() -> None:
    backend, cfg = finished()
    out = io.StringIO()
    render(run_checks(cfg, backend), out, True)
    parsed = orjson.loads(out.getvalue())
    assert len(parsed) == 10
    assert set(parsed[0]) == {"name", "level", "detail", "hint"}
    assert parsed[0]["name"] == "LHM"


def mutate_lhm_missing(backend: FakeBackend, cfg: Config) -> None:
    backend.json_by_url.pop(LHM_DATA_URL)


def mutate_health_without_lhm(backend: FakeBackend, cfg: Config) -> None:
    backend.json_by_url[HEALTH_URL] = {"ok": True, "version": "0.1.0", "session": None}


def mutate_lhm_false(backend: FakeBackend, cfg: Config) -> None:
    backend.json_by_url[HEALTH_URL] = {"ok": True, "lhm": False}


def mutate_domain_network(backend: FakeBackend, cfg: Config) -> None:
    backend.profile = "DomainAuthenticated"


def mutate_public_network(backend: FakeBackend, cfg: Config) -> None:
    backend.profile = "Public"


def mutate_rule_disabled(backend: FakeBackend, cfg: Config) -> None:
    rule = backend.rules["FluentMon"]
    backend.rules["FluentMon"] = FirewallRule(rule.name, rule.port, rule.profile, rule.remote, rule.program, False)


def mutate_rule_program(backend: FakeBackend, cfg: Config) -> None:
    rule = backend.rules["FluentMon"]
    backend.rules["FluentMon"] = FirewallRule(rule.name, rule.port, rule.profile, rule.remote, "C:/old/pythonw.exe")


def mutate_rule_removed(backend: FakeBackend, cfg: Config) -> None:
    backend.rules.pop("FluentMon")


def mutate_port_closed(backend: FakeBackend, cfg: Config) -> None:
    backend.reachable.clear()


def mutate_no_lan(backend: FakeBackend, cfg: Config) -> None:
    backend.lan = None


def mutate_folder_missing(backend: FakeBackend, cfg: Config) -> None:
    backend.dirs.discard("C:/FluentMon/runs")


def mutate_task_never_run(backend: FakeBackend, cfg: Config) -> None:
    backend.task_unregister("FluentMon LHM")
    backend.task_register("FluentMon LHM", backend.program_path(), "", True, 1)


def mutate_task_program_differs(backend: FakeBackend, cfg: Config) -> None:
    backend.task_unregister("FluentMon")
    backend.task_register("FluentMon", Path("C:/old/pythonw.exe"), "-m fluentmon", False, 1)
    backend.task_start("FluentMon")


def mutate_token_short(backend: FakeBackend, cfg: Config) -> None:
    cfg.server.token = "short"


def mutate_no_ansys(backend: FakeBackend, cfg: Config) -> None:
    backend.ansys = None


def mutate_sensor_dash(backend: FakeBackend, cfg: Config) -> None:
    backend.json_by_url[LHM_DATA_URL] = make_tree(AMD, {"Powers": {"Package": "-"}})


CASES: list[tuple[str, Callable[[FakeBackend, Config], None], dict[str, str], int]] = [
    ("lhm missing", mutate_lhm_missing, {"LHM": "fail", "Sensors": "fail"}, 1),
    ("health without lhm field", mutate_health_without_lhm, {"Server": "warn"}, 0),
    ("health lhm false", mutate_lhm_false, {"Server": "fail"}, 1),
    ("domain network", mutate_domain_network, {"Network profile": "warn"}, 0),
    ("public network", mutate_public_network, {"Network profile": "warn"}, 0),
    ("rule disabled", mutate_rule_disabled, {"Firewall": "fail"}, 1),
    ("rule program differs", mutate_rule_program, {"Firewall": "fail"}, 1),
    ("rule removed", mutate_rule_removed, {"Firewall": "fail"}, 1),
    ("port closed", mutate_port_closed, {"Port": "fail"}, 1),
    ("no LAN address", mutate_no_lan, {"Port": "fail"}, 1),
    ("folder missing", mutate_folder_missing, {"Folders": "fail"}, 1),
    ("task never run", mutate_task_never_run, {"Tasks": "fail"}, 1),
    ("token too short", mutate_token_short, {"Config": "fail"}, 1),
    ("app task program differs", mutate_task_program_differs, {"Tasks": "warn"}, 0),
    ("no ANSYS install", mutate_no_ansys, {"Fluent": "warn"}, 0),
    ("sensor value dash", mutate_sensor_dash, {"Sensors": "fail"}, 1),
]


@pytest.mark.parametrize(("label", "mutate", "changed", "code"), CASES, ids=[c[0] for c in CASES])
def test_one_condition_changes_only_its_check(
    label: str,
    mutate: Callable[[FakeBackend, Config], None],
    changed: dict[str, str],
    code: int,
) -> None:
    backend, cfg = finished()
    mutate(backend, cfg)
    checks = run_checks(cfg, backend)
    expected = {name: "pass" for name in NAMES}
    expected.update(changed)
    assert levels(checks) == expected
    assert exit_code(checks) == code
    for check in checks:
        if check.level != "pass":
            assert check.hint


def test_config_error_fails_only_config_check() -> None:
    backend, cfg = finished()
    checks = run_checks(cfg, backend, config_error="config.toml: unexpected token")
    assert levels(checks)["Config"] == "fail"
    assert [c.name for c in checks if c.level != "pass"] == ["Config"]
    assert [c for c in checks if c.name == "Config"][0].detail == "config.toml: unexpected token"


def test_raising_backend_call_fails_only_its_check() -> None:
    backend, cfg = finished()
    backend.fail_on("fw_get", OSError("boom"))
    checks = run_checks(cfg, backend)
    assert levels(checks) == {**{name: "pass" for name in NAMES}, "Firewall": "fail"}
    firewall = [c for c in checks if c.name == "Firewall"][0]
    assert firewall.detail == "boom"
    assert exit_code(checks) == 1


def test_no_sensors_configured_warns_with_none_configured() -> None:
    backend, cfg = finished()
    cfg.meters.power_sensor = None
    cfg.meters.temp_sensor = None
    cfg.meters.clock_sensors = []
    checks = run_checks(cfg, backend)
    sensors = [c for c in checks if c.name == "Sensors"][0]
    assert sensors.level == "warn"
    assert sensors.detail == "none configured"
    assert sensors.hint == "run fmon setup again to detect the sensors"
    assert exit_code(checks) == 0


def test_check_is_frozen() -> None:
    check = Check("LHM", "pass", "ok", None)
    with pytest.raises(AttributeError):
        check.level = "fail"
