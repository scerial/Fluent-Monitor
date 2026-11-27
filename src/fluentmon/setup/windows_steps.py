from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from fluentmon.config import ConfigError, load_config
from fluentmon.doctor import exit_code, render, run_checks
from fluentmon.pairing import print_pairing
from fluentmon.setup import pins
from fluentmon.setup.backend import FirewallRule
from fluentmon.setup.runner import Context, StepError

LHM_TASK = "FluentMon LHM"
APP_TASK = "FluentMon"
RULE_NAME = "FluentMon"
LHM_PORT = 8085
OWNERS_ALLOWED = frozenset({"pythonw.exe", "python.exe", "librehardwaremonitor.exe"})


class LhmTaskStep:
    id = "task_lhm"
    undoable = True

    def done(self, ctx: Context) -> bool:
        return (
            ctx.backend.task_exists(LHM_TASK)
            and ctx.backend.task_last_result(LHM_TASK) is not None
            and ctx.backend.http_get_json(pins.LHM_DATA_URL) is not None
        )

    def run(self, ctx: Context) -> dict:
        backend = ctx.backend
        if backend.task_exists(LHM_TASK):
            backend.task_stop(LHM_TASK)
        backend.task_register(LHM_TASK, pins.INSTALL_DIR / pins.LHM_EXE, "", True, 1)
        try:
            backend.task_start(LHM_TASK)
        except OSError as exc:
            raise StepError(f"cannot start task {LHM_TASK}: {exc}") from exc
        return {"task": LHM_TASK}

    def undo(self, ctx: Context, entry: dict) -> None:
        ctx.backend.task_stop(LHM_TASK)
        ctx.backend.task_unregister(LHM_TASK)


class AppTaskStep:
    id = "task_app"
    undoable = True

    def done(self, ctx: Context) -> bool:
        return ctx.backend.task_exists(APP_TASK) and ctx.backend.task_last_result(APP_TASK) is not None

    def run(self, ctx: Context) -> dict:
        backend = ctx.backend
        program = backend.program_path()
        backend.task_register(APP_TASK, program, "-m fluentmon", False, 1)
        try:
            backend.task_start(APP_TASK)
        except OSError as exc:
            raise StepError(f"cannot start task {APP_TASK}: {exc}") from exc
        return {"task": APP_TASK, "program": str(program)}

    def undo(self, ctx: Context, entry: dict) -> None:
        ctx.backend.task_stop(APP_TASK)
        ctx.backend.task_unregister(APP_TASK)


class FirewallStep:
    id = "firewall"
    undoable = True

    def __init__(self, port: int = 8765) -> None:
        self._port = port

    def _wanted(self, ctx: Context) -> FirewallRule:
        return FirewallRule(RULE_NAME, self._port, "Private", "LocalSubnet", str(ctx.backend.program_path()))

    def done(self, ctx: Context) -> bool:
        rule = ctx.backend.fw_get(RULE_NAME)
        wanted = self._wanted(ctx)
        return (
            rule is not None
            and rule.enabled
            and rule.port == wanted.port
            and rule.profile == wanted.profile
            and rule.program == wanted.program
            and rule.remote.startswith("LocalSubnet")
        )

    def run(self, ctx: Context) -> dict:
        ctx.backend.fw_set(self._wanted(ctx))
        return {"rule": RULE_NAME, "ranges": ["LocalSubnet"]}

    def undo(self, ctx: Context, entry: dict) -> None:
        ctx.backend.fw_remove(RULE_NAME)


def _is_lhm_on_http_sys(ctx: Context, port: int, owner: str) -> bool:
    return (
        port == LHM_PORT
        and owner.lower() == "system"
        and ctx.backend.http_get_json(pins.LHM_DATA_URL) is not None
    )


class PreflightStep:
    id = "preflight"
    undoable = False

    def __init__(self, port: int = 8765) -> None:
        self._port = port

    def done(self, ctx: Context) -> bool:
        return False

    def run(self, ctx: Context) -> dict:
        backend = ctx.backend
        if not backend.is_windows():
            raise StepError("fmon setup runs on Windows 10 or 11 only")
        for port in (self._port, LHM_PORT):
            owner = backend.port_owner(port)
            if owner is not None and owner.lower() not in OWNERS_ALLOWED and not _is_lhm_on_http_sys(ctx, port, owner):
                raise StepError(f"port {port} is held by {owner}")
        if ctx.lhm_zip is None and not backend.can_connect("github.com", 443):
            raise StepError("github.com is not reachable on port 443")
        return {}


class FinishStep:
    id = "finish"
    undoable = False

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

    def done(self, ctx: Context) -> bool:
        return False

    def run(self, ctx: Context) -> dict:
        if ctx.dry_run:
            ctx.out.write("would run fmon doctor and print the pairing code\n")
            return {}
        try:
            cfg = load_config(ctx.config_path)
        except ConfigError as exc:
            raise StepError(f"config cannot be read: {exc}") from exc
        checks = []
        for attempt in range(10):
            checks = run_checks(cfg, ctx.backend)
            server = next(c for c in checks if c.name == "Server")
            if server.level != "fail":
                break
            if attempt < 9:
                self._sleep(2.0)
        render(checks, ctx.out, False)
        if exit_code(checks) == 1:
            raise StepError("fmon doctor reports failures")
        print_pairing(cfg, ctx.out)
        return {}
