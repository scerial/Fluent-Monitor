from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from fluentmon import __version__
from fluentmon.app import build_app
from fluentmon.config import load_config
from fluentmon.launcher import RunSpec, launch
from fluentmon.setup.backend import Backend
from fluentmon.setup.commands import default_config_path, run_doctor, run_setup, run_uninstall

INSTALLER_COMMANDS = ("setup", "doctor", "uninstall")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fmon")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--record", type=Path, default=None)
    serve.add_argument("--config", type=Path, default=None)
    start = commands.add_parser("launch")
    start.add_argument("case", type=Path)
    start.add_argument("--cores", type=int, required=True)
    start.add_argument("--fluent-exe", type=Path, default=Path("fluent"))
    start.add_argument("--config", type=Path, default=None)
    commands.add_parser("version")
    return parser


def _attach_missing_streams() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = default_config_path().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "server.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _serve(args: argparse.Namespace) -> int:
    _attach_missing_streams()
    cfg = load_config(args.config)
    if not cfg.server.token:
        print("refusing to start: config.toml has no [server] token", file=sys.stderr)
        return 2
    app = build_app(cfg, args.record)
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        ws_ping_interval=15,
        ws_ping_timeout=20,
    )
    return 0


def _installer(values: list[str], backend: Backend) -> int:
    command, rest = values[0], values[1:]
    if command == "setup":
        return run_setup(rest, backend, input, sys.stdout)
    if command == "doctor":
        return run_doctor(rest, backend, sys.stdout)
    return run_uninstall(rest, backend, input, sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in INSTALLER_COMMANDS:
        from fluentmon.setup.winbackend import WindowsBackend

        return _installer(values, WindowsBackend())
    args = build_parser().parse_args(values)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "launch":
        cfg = load_config(args.config)
        spec = RunSpec(case=args.case, cores=args.cores, fluent_exe=args.fluent_exe)
        return launch(cfg, spec)
    return _serve(args)
