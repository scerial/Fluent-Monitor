from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import orjson

from fluentmon.config import Config
from fluentmon.setup.backend import Backend
from fluentmon.system.power import find_sensor

TASK_RUNNING = 267009
TASK_OK_RESULTS = frozenset({0, TASK_RUNNING})

HINTS: dict[str, str] = {
    "LHM": "start LibreHardwareMonitor and turn on its web server on 127.0.0.1:8085",
    "Sensors": "run fmon setup again to detect the sensors",
    "Server": "start the FluentMon task, or run fmon launch",
    "Port": "check that this PC has a LAN address and that the firewall allows the server port",
    "Network profile": "switch the network connection to Private",
    "Firewall": "run fmon setup to recreate the FluentMon firewall rule",
    "Tasks": "run fmon setup, or start the scheduled tasks FluentMon and FluentMon LHM",
    "Folders": "create the watch and runs folders, or fix their permissions",
    "Config": "run fmon setup to rewrite config.toml",
    "Fluent": "install ANSYS Fluent, or set ANSYS_INC",
}


@dataclass(frozen=True)
class Check:
    name: str
    level: str
    detail: str
    hint: str | None = None


def _pass(name: str, detail: str) -> Check:
    return Check(name, "pass", detail, None)


def _fail(name: str, detail: str) -> Check:
    return Check(name, "fail", detail, HINTS[name])


def _warn(name: str, detail: str) -> Check:
    return Check(name, "warn", detail, HINTS[name])


def _lhm(cfg: Config, backend: Backend, state: dict) -> Check:
    url = cfg.meters.lhm_url.rstrip("/") + "/data.json"
    tree = backend.http_get_json(url)
    if not isinstance(tree, dict):
        return _fail("LHM", f"no answer from {url}")
    state["tree"] = tree
    return _pass("LHM", f"{url} answered")


def _sensors(cfg: Config, backend: Backend, state: dict) -> Check:
    meters = cfg.meters
    source = meters.power_sensor or meters.temp_sensor
    if not meters.power_sensor and not meters.temp_sensor and not meters.clock_sensors:
        return _warn("Sensors", "none configured")
    tree = state.get("tree")
    if tree is None:
        return _fail("Sensors", "LHM is not answering, so the sensors cannot be read")
    targets = [p for p in (meters.power_sensor, meters.temp_sensor) if p]
    if meters.clock_sensors:
        if source is None:
            return _fail("Sensors", "clock sensors are configured but no hardware path is known")
        hardware = source.split("/")[0]
        targets += [f"{hardware}/Clocks/{name}" for name in meters.clock_sensors]
    readings: list[str] = []
    missing: list[str] = []
    for path in targets:
        value = find_sensor(tree, path)
        if value is None:
            missing.append(path)
        else:
            readings.append(f"{path}={value:g}")
    if missing:
        return _fail("Sensors", "not numeric in LHM data: " + ", ".join(missing))
    return _pass("Sensors", ", ".join(readings))


def _server(cfg: Config, backend: Backend, state: dict) -> Check:
    url = f"http://127.0.0.1:{cfg.server.port}/api/health"
    health = backend.http_get_json(url)
    if not isinstance(health, dict):
        return _fail("Server", f"no answer from {url}")
    if health.get("ok") is not True:
        return _fail("Server", "health does not report ok")
    if "lhm" not in health:
        return _warn("Server", "health has no lhm field")
    if health["lhm"] is True:
        return _pass("Server", "health ok, LHM connected")
    return _fail("Server", "health ok, but LHM is not connected")


def _port(cfg: Config, backend: Backend, state: dict) -> Check:
    lan = backend.lan_address()
    if lan is None:
        return _fail("Port", "no LAN address found")
    if backend.can_connect(lan, cfg.server.port):
        return _pass("Port", f"{lan}:{cfg.server.port} accepts connections")
    return _fail("Port", f"{lan}:{cfg.server.port} does not accept connections")


def _network(cfg: Config, backend: Backend, state: dict) -> Check:
    profile = backend.network_profile()
    if profile == "Private":
        return _pass("Network profile", "Private")
    return _warn("Network profile", f"network profile is {profile}")


def _firewall(cfg: Config, backend: Backend, state: dict) -> Check:
    rule = backend.fw_get("FluentMon")
    if rule is None:
        return _fail("Firewall", "rule FluentMon not found")
    problems: list[str] = []
    if not rule.enabled:
        problems.append("rule is disabled")
    if "Private" not in rule.profile:
        problems.append(f"profile is {rule.profile}")
    if rule.port != cfg.server.port:
        problems.append(f"port is {rule.port}")
    if rule.program != str(backend.program_path()):
        problems.append("program path differs from the running program")
    if problems:
        return _fail("Firewall", "; ".join(problems))
    return _pass("Firewall", f"rule FluentMon is enabled on {rule.profile} for port {rule.port}")


def _tasks(cfg: Config, backend: Backend, state: dict) -> Check:
    problems: list[str] = []
    for name in ("FluentMon", "FluentMon LHM"):
        if not backend.task_exists(name):
            problems.append(f"{name} is not registered")
            continue
        result = backend.task_last_result(name)
        if result is None:
            problems.append(f"{name} has never run")
        elif result not in TASK_OK_RESULTS:
            problems.append(f"{name} last result is {result}")
    if problems:
        return _fail("Tasks", "; ".join(problems))
    program = backend.task_program("FluentMon")
    if program is None or str(program) != str(backend.program_path()):
        return Check(
            "Tasks",
            "warn",
            "FluentMon task runs a different program than this installation",
            "run fmon setup again after an upgrade",
        )
    return _pass("Tasks", "FluentMon and FluentMon LHM are registered, running or finished with result 0")


def _folders(cfg: Config, backend: Backend, state: dict) -> Check:
    folders = [cfg.server.watch_dir, cfg.server.runs_dir]
    bad = [f for f in folders if not (backend.exists(Path(f)) and backend.is_writable(Path(f)))]
    if bad:
        return _fail("Folders", "missing or not writable: " + ", ".join(bad))
    return _pass("Folders", " and ".join(folders) + " are writable")


def _config(cfg: Config, backend: Backend, state: dict, config_error: str | None) -> Check:
    if config_error is not None:
        return _fail("Config", config_error)
    if len(cfg.server.token) < 32:
        return _fail("Config", "token is shorter than 32 characters")
    return _pass("Config", "config parses; token has 32 characters or more")


def _fluent(cfg: Config, backend: Backend, state: dict) -> Check:
    found = backend.find_ansys()
    if found is None:
        return _warn("Fluent", "no ANSYS install found under ANSYS_INC or Program Files")
    return _pass("Fluent", f"ANSYS install found at {found}")


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:
        return _fail(name, str(exc))


def run_checks(cfg: Config, backend: Backend, config_error: str | None = None) -> list[Check]:
    state: dict = {}
    return [
        _guard("LHM", lambda: _lhm(cfg, backend, state)),
        _guard("Sensors", lambda: _sensors(cfg, backend, state)),
        _guard("Server", lambda: _server(cfg, backend, state)),
        _guard("Port", lambda: _port(cfg, backend, state)),
        _guard("Network profile", lambda: _network(cfg, backend, state)),
        _guard("Firewall", lambda: _firewall(cfg, backend, state)),
        _guard("Tasks", lambda: _tasks(cfg, backend, state)),
        _guard("Folders", lambda: _folders(cfg, backend, state)),
        _guard("Config", lambda: _config(cfg, backend, state, config_error)),
        _guard("Fluent", lambda: _fluent(cfg, backend, state)),
    ]


def render(checks: Sequence[Check], out: IO[str], as_json: bool) -> None:
    if as_json:
        payload = [
            {"name": c.name, "level": c.level, "detail": c.detail, "hint": c.hint}
            for c in checks
        ]
        out.write(orjson.dumps(payload).decode("utf-8") + "\n")
        return
    for c in checks:
        line = f"{c.level:<4} {c.name:<16} {c.detail}"
        if c.level != "pass" and c.hint:
            line += f" -- {c.hint}"
        out.write(line + "\n")


def exit_code(checks: Sequence[Check]) -> int:
    return 1 if any(c.level == "fail" for c in checks) else 0
