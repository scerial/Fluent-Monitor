from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import tomllib
import tomli_w

log = logging.getLogger("fluentmon.config")

SECTIONS = ("server", "convergence", "meters")

ABSENT_AS_NONE: dict[tuple[str, str], Any] = {
    ("meters", "power_sensor"): None,
    ("meters", "temp_sensor"): None,
    ("meters", "clock_sensors"): [],
}


class ConfigError(ValueError):
    pass


@dataclass
class ServerCfg:
    port: int = 8765
    token: str = ""
    watch_dir: str = "C:/FluentMon/transcripts"
    runs_dir: str = "C:/FluentMon/runs"
    host: str = "0.0.0.0"
    safety_read_s: float = 0.0
    allow_control: bool = False
    ntfy_topic: str = ""


@dataclass
class ConvergenceCfg:
    target: float = 1e-6
    exclude: list[str] = field(default_factory=list)
    extra_residual_names: list[str] = field(default_factory=list)


@dataclass
class MetersCfg:
    cpus: list[int] = field(default_factory=lambda: [0, 4, 8, 12])
    pin_ranks: bool = False
    lhm_url: str = "http://127.0.0.1:8085"
    power_sensor: str | None = "AMD Ryzen 7 7800X3D/Powers/Package"
    pkg_max_w: float = 162.0
    clock_sensors: list[str] = field(
        default_factory=lambda: [
            "Core #1 (Effective)",
            "Core #3 (Effective)",
            "Core #5 (Effective)",
            "Core #7 (Effective)",
        ]
    )
    temp_sensor: str | None = "AMD Ryzen 7 7800X3D/Temperatures/Core (Tctl/Tdie)"
    temp_base_c: float = 20.0
    temp_warn_c: float = 70.0
    tjmax_c: float = 89.0
    tdp_w: float = 120.0
    poll_min_ms: int = 200
    idle_poll_s: float = 5.0
    fluent_ram_s: float = 1.0


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    convergence: ConvergenceCfg = field(default_factory=ConvergenceCfg)
    meters: MetersCfg = field(default_factory=MetersCfg)
    source: Path | None = field(default=None, compare=False)


def find_config(explicit: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env_value = os.environ.get("FLUENTMON_CONFIG")
    if env_value:
        candidates.append(Path(env_value))
    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.append(Path(program_data) / "FluentMon" / "config.toml")
    candidates.append(Path.cwd() / "config.toml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _check(value: Any, type_name: str) -> Any:
    if type_name == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(type_name)
        return value
    if type_name == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(type_name)
        return float(value)
    if type_name == "bool":
        if not isinstance(value, bool):
            raise TypeError(type_name)
        return value
    if type_name in ("str", "str | None"):
        if not isinstance(value, str):
            raise TypeError(type_name)
        return value
    if type_name == "list[int]":
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) for v in value):
            raise TypeError(type_name)
        return list(value)
    if type_name == "list[str]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise TypeError(type_name)
        return list(value)
    raise TypeError(type_name)


def _build(section: str, cls: type, raw: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(cls)}
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            log.warning("unknown key %s.%s ignored", section, key)
            continue
        try:
            values[key] = _check(value, known[key].type)
        except TypeError:
            raise ConfigError(f"{section}.{key} must be {known[key].type}") from None
    for name in known:
        if name not in values and (section, name) in ABSENT_AS_NONE:
            absent = ABSENT_AS_NONE[(section, name)]
            values[name] = list(absent) if isinstance(absent, list) else absent
    return cls(**values)


def _validate(cfg: Config) -> None:
    if not cfg.convergence.target > 0:
        raise ConfigError("convergence.target must be above 0")
    for cpu in cfg.meters.cpus:
        if cpu < 0:
            raise ConfigError("meters.cpus must hold non-negative integers")


def load_config(path: Path | None = None) -> Config:
    found = find_config(path)
    if found is None:
        return Config()
    try:
        with found.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{found}: {exc}") from exc
    for name in data:
        if name not in SECTIONS:
            log.warning("unknown section %s ignored", name)
    for name in SECTIONS:
        if not isinstance(data.get(name, {}), dict):
            raise ConfigError(f"{name} must be a table")
    cfg = Config(
        server=_build("server", ServerCfg, data.get("server", {})),
        convergence=_build("convergence", ConvergenceCfg, data.get("convergence", {})),
        meters=_build("meters", MetersCfg, data.get("meters", {})),
        source=found,
    )
    _validate(cfg)
    return cfg


def save_config(cfg: Config, path: Path) -> None:
    document: dict[str, Any] = {}
    for name in SECTIONS:
        section = asdict(getattr(cfg, name))
        document[name] = {key: value for key, value in section.items() if value is not None}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            tomli_w.dump(document, handle)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
