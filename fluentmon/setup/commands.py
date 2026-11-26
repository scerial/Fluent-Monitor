from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, NoReturn

from fluentmon.config import Config, ConfigError, load_config
from fluentmon.doctor import exit_code, render, run_checks
from fluentmon.setup.backend import Backend
from fluentmon.setup.configstep import ConfigStep
from fluentmon.setup.confirm import DetectStep
from fluentmon.setup.fake import DryRunBackend
from fluentmon.setup.lhm import LhmSettingsStep, LhmStep
from fluentmon.setup.manifest import Manifest, ManifestError
from fluentmon.setup.runner import Context, Step, run_steps, undo_steps
from fluentmon.setup.tailscale import TailscaleStep
from fluentmon.setup.windows_steps import (
    AppTaskStep,
    FinishStep,
    FirewallStep,
    LhmTaskStep,
    PreflightStep,
)

DEFAULT_PORT = 8765


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def default_config_path() -> Path:
    program_data = os.environ.get("ProgramData", "C:\\ProgramData")
    return Path(program_data) / "FluentMon" / "config.toml"


def build_steps(ask: Callable[[str], str], port: int = DEFAULT_PORT) -> list[Step]:
    return [
        PreflightStep(port),
        LhmStep(),
        LhmSettingsStep(),
        LhmTaskStep(),
        DetectStep(ask),
        ConfigStep(),
        FirewallStep(port),
        AppTaskStep(),
        TailscaleStep(ask),
        FinishStep(),
    ]


def _setup_parser() -> _Parser:
    parser = _Parser(prog="fmon setup")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--rotate-token", action="store_true")
    parser.add_argument("--lhm-zip", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    return parser


def _doctor_parser() -> _Parser:
    parser = _Parser(prog="fmon doctor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config", type=Path, default=None)
    return parser


def _uninstall_parser() -> _Parser:
    parser = _Parser(prog="fmon uninstall")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--purge", action="store_true")
    return parser


def run_setup(argv: Sequence[str], backend: Backend, ask: Callable[[str], str], out: IO[str]) -> int:
    try:
        args = _setup_parser().parse_args(list(argv))
    except UsageError as exc:
        out.write(f"fmon setup: {exc}\n")
        return 2
    config_path = args.config or default_config_path()
    if args.lhm_zip is not None and not backend.exists(args.lhm_zip):
        out.write(f"fmon setup: --lhm-zip file not found: {args.lhm_zip}\n")
        return 2
    if not args.dry_run and not backend.is_admin():
        return backend.relaunch_elevated(["setup", *argv])
    run_backend: Backend = DryRunBackend(backend, out) if args.dry_run else backend
    port = DEFAULT_PORT
    try:
        if backend.exists(config_path):
            port = load_config(config_path).server.port
    except ConfigError as exc:
        out.write(f"fmon setup: config cannot be read: {exc}\n")
        return 2
    folder = config_path.parent
    manifest = Manifest(folder / "setup-state.json", run_backend)
    try:
        manifest.load()
    except ManifestError as exc:
        out.write(f"fmon setup: {exc}\n")
        return 2
    ctx = Context(
        backend=run_backend,
        manifest=manifest,
        out=out,
        config_path=config_path,
        log_path=folder / "logs" / "setup.log",
        yes=args.yes,
        rotate_token=args.rotate_token,
        lhm_zip=args.lhm_zip,
        dry_run=args.dry_run,
    )
    return run_steps(build_steps(ask, port), ctx)


def run_doctor(argv: Sequence[str], backend: Backend, out: IO[str]) -> int:
    try:
        args = _doctor_parser().parse_args(list(argv))
    except UsageError as exc:
        out.write(f"fmon doctor: {exc}\n")
        return 2
    config_path = args.config or default_config_path()
    cfg = Config()
    config_error: str | None = None
    if backend.exists(config_path):
        try:
            cfg = load_config(config_path)
        except ConfigError as exc:
            config_error = str(exc)
    else:
        config_error = f"{config_path} does not exist"
    checks = run_checks(cfg, backend, config_error)
    render(checks, out, args.json)
    return exit_code(checks)


def run_uninstall(argv: Sequence[str], backend: Backend, ask: Callable[[str], str], out: IO[str]) -> int:
    try:
        args = _uninstall_parser().parse_args(list(argv))
    except UsageError as exc:
        out.write(f"fmon uninstall: {exc}\n")
        return 2
    folder = default_config_path().parent
    manifest = Manifest(folder / "setup-state.json", backend)
    try:
        manifest.load()
    except ManifestError as exc:
        out.write(f"fmon uninstall: {exc}\n")
        return 2
    steps = build_steps(ask)
    pending = [s for s in steps if s.undoable and _recorded_done(manifest, s.id)]
    if not pending:
        out.write("Nothing to undo.\n")
        return 0
    if not args.yes:
        out.write("Steps: " + ", ".join(s.id for s in reversed(pending)) + "\n")
        try:
            answer = ask("Undo these steps? [y/N]")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out.write("Nothing changed.\n")
            return 0
    ctx = Context(
        backend=backend,
        manifest=manifest,
        out=out,
        config_path=folder / "config.toml",
        log_path=folder / "logs" / "setup.log",
        purge=args.purge,
    )
    status = undo_steps(steps, ctx)
    if args.purge and status == 0:
        backend.rmtree(folder)
        out.write("uv tool uninstall fluentmon\n")
    return status


def _recorded_done(manifest: Manifest, step_id: str) -> bool:
    entry = manifest.get(step_id)
    return entry is not None and bool(entry.get("done"))
