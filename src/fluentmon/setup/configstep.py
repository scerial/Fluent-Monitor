from __future__ import annotations

import copy
import secrets
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, fields
from pathlib import Path

import tomli_w

from fluentmon.config import Config, ConvergenceCfg, MetersCfg, ServerCfg
from fluentmon.setup.runner import Context, StepError

SERVER_KEYS: tuple[str, ...] = ("watch_dir", "runs_dir")
METER_KEYS: frozenset[str] = frozenset(f.name for f in fields(MetersCfg))
SECTION_KEYS: dict[str, frozenset[str]] = {
    "server": frozenset(f.name for f in fields(ServerCfg)),
    "convergence": frozenset(f.name for f in fields(ConvergenceCfg)),
    "meters": METER_KEYS,
}
ABSENT_VALUES: dict[str, object] = {
    "power_sensor": None,
    "temp_sensor": None,
    "clock_sensors": [],
}


def build_config(values: Mapping[str, object], token: str, existing: Config | None) -> Config:
    cfg = copy.deepcopy(existing) if existing is not None else Config()
    known = {f.name for f in fields(MetersCfg)}
    for key in values:
        if key not in known and key not in SERVER_KEYS:
            raise ValueError(f"unknown detected key: {key}")
    meter_values: dict[str, object] = {}
    for f in fields(MetersCfg):
        if f.name in values:
            meter_values[f.name] = values[f.name]
        elif f.name in ABSENT_VALUES:
            absent = ABSENT_VALUES[f.name]
            meter_values[f.name] = list(absent) if isinstance(absent, list) else absent
        else:
            meter_values[f.name] = getattr(cfg.meters, f.name)
    cfg.meters = MetersCfg(**meter_values)
    for key in SERVER_KEYS:
        if key in values:
            setattr(cfg.server, key, values[key])
    cfg.server.token = token
    return cfg


def config_text(cfg: Config) -> str:
    document: dict[str, dict] = {}
    for name in ("server", "convergence", "meters"):
        section = asdict(getattr(cfg, name))
        document[name] = {key: value for key, value in section.items() if value is not None}
    return tomli_w.dumps(document)


class ConfigStep:
    id = "config"
    undoable = True

    def __init__(self, new_token: Callable[[], str] = lambda: secrets.token_urlsafe(24)) -> None:
        self._new_token = new_token

    def done(self, ctx: Context) -> bool:
        return (
            ctx.manifest.get(self.id) is not None
            and not ctx.rotate_token
            and ctx.backend.exists(ctx.config_path)
        )

    def run(self, ctx: Context) -> dict:
        backend = ctx.backend
        path = ctx.config_path
        detect = ctx.manifest.get("detect")
        if detect is None:
            raise StepError("detect has not run")
        values = detect["values"]

        existing = backend.read_bytes(path)
        mode = "created" if existing is None else "updated"
        kept: object = None
        existing_cfg: Config | None = None
        if existing is not None:
            try:
                parsed = tomllib.loads(existing.decode("utf-8"))
                kept = parsed.get("server", {}).get("token")
                existing_cfg = Config()
                for section, known in SECTION_KEYS.items():
                    target = getattr(existing_cfg, section)
                    for key, value in parsed.get(section, {}).items():
                        if key in known:
                            setattr(target, key, value)
            except (tomllib.TOMLDecodeError, UnicodeDecodeError, AttributeError):
                ctx.log(self.id, "replace", f"{path} could not be parsed; backed up and replaced")
                kept = None
                existing_cfg = None
        token = kept if isinstance(kept, str) and len(kept) >= 32 and not ctx.rotate_token else None
        if token is None:
            token = self._new_token()

        cfg = build_config(values, token, existing_cfg)
        text = config_text(cfg).encode("utf-8")

        backup: Path | None = None
        if existing is not None:
            stamp = backend.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.bak-{stamp}")
            backend.copy(path, backup)

        backend.mkdir(path.parent)
        backend.mkdir(Path(cfg.server.watch_dir))
        backend.mkdir(Path(cfg.server.runs_dir))
        backend.write_bytes(path, text)
        backend.restrict_acl(path)
        return {"path": str(path), "mode": mode, "backup": None if backup is None else str(backup)}

    def undo(self, ctx: Context, entry: dict) -> None:
        if ctx.purge:
            ctx.backend.delete(Path(entry["path"]))
