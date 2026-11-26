import io
from pathlib import Path

import pytest

from fluentmon.setup import pins
from fluentmon.setup.backend import FirewallRule
from fluentmon.setup.fake import FakeBackend
from fluentmon.setup.manifest import Manifest
from fluentmon.setup.runner import Context, StepError, run_steps, undo_steps
from fluentmon.setup.tailscale import SERVE_COMMAND, TAILSCALE_RANGE, TailscaleStep
from fluentmon.setup.windows_steps import APP_TASK, LHM_TASK, RULE_NAME, AppTaskStep, FirewallStep, LhmTaskStep

CONFIG = Path("C:/ProgramData/FluentMon/config.toml")
LOG = Path("C:/ProgramData/FluentMon/logs/setup.log")
MUTATING_TASKS = {"task_register", "task_start", "task_stop", "task_unregister", "fw_set", "fw_remove"}


def make_ctx(backend: FakeBackend, out: io.StringIO | None = None, yes: bool = False, dry_run: bool = False) -> Context:
    manifest = Manifest(Path("C:/ProgramData/FluentMon/setup-state.json"), backend)
    manifest.load()
    return Context(
        backend=backend,
        manifest=manifest,
        out=out or io.StringIO(),
        config_path=CONFIG,
        log_path=LOG,
        yes=yes,
        dry_run=dry_run,
    )


def steps_all() -> list:
    return [LhmTaskStep(), AppTaskStep(), FirewallStep()]


def scripted(answers: list[str], asked: list[str] | None = None):
    items = iter(answers)

    def ask(prompt: str) -> str:
        if asked is not None:
            asked.append(prompt)
        try:
            return next(items)
        except StopIteration:
            raise EOFError

    return ask


def mutating(backend: FakeBackend) -> list[str]:
    return [name for name, _ in backend.calls if name in MUTATING_TASKS]


def test_lhm_task_registered_highest_restart_and_started() -> None:
    backend = FakeBackend()
    assert run_steps([LhmTaskStep()], make_ctx(backend)) == 0
    task = backend.tasks[LHM_TASK]
    assert task["highest"] is True
    assert task["restart_minutes"] == 1
    assert task["program"] == str(pins.INSTALL_DIR / pins.LHM_EXE)
    assert task["last_result"] == 0


def test_app_task_uses_program_path_and_module_arguments() -> None:
    backend = FakeBackend()
    run_steps([AppTaskStep()], make_ctx(backend))
    task = backend.tasks[APP_TASK]
    assert task["program"] == str(backend.program)
    assert task["args"] == "-m fluentmon"
    assert task["highest"] is False
    assert task["restart_minutes"] == 1


def test_firewall_rule_fields() -> None:
    backend = FakeBackend()
    run_steps([FirewallStep()], make_ctx(backend))
    rule = backend.rules[RULE_NAME]
    assert rule.profile == "Private"
    assert rule.remote == "LocalSubnet"
    assert rule.program == str(backend.program)
    assert rule.port == 8765
    assert rule.enabled is True


def test_program_change_makes_firewall_not_done_and_rerun_updates() -> None:
    backend = FakeBackend()
    run_steps([FirewallStep()], make_ctx(backend))
    backend.program = Path("C:/Users/tester/uv/tools/fluentmon-2/pythonw.exe")
    assert FirewallStep().done(make_ctx(backend)) is False
    run_steps([FirewallStep()], make_ctx(backend))
    assert backend.rules[RULE_NAME].program == str(backend.program)


def test_registered_but_never_run_task_is_not_done() -> None:
    backend = FakeBackend()
    backend.task_register(LHM_TASK, pins.INSTALL_DIR / pins.LHM_EXE, "", True, 1)
    manifest_ctx = make_ctx(backend)
    assert LhmTaskStep().done(manifest_ctx) is False
    run_steps([LhmTaskStep()], make_ctx(backend))
    assert backend.tasks[LHM_TASK]["last_result"] == 0


def test_task_start_failure_names_task_and_leaves_it_registered() -> None:
    backend = FakeBackend()
    backend.fail_on("task_start", OSError("access denied"))
    with pytest.raises(StepError, match="cannot start task FluentMon LHM"):
        LhmTaskStep().run(make_ctx(backend))
    assert backend.task_exists(LHM_TASK)


def test_full_run_mutating_calls_in_order() -> None:
    backend = FakeBackend()
    assert run_steps(steps_all(), make_ctx(backend)) == 0
    assert mutating(backend) == [
        "task_register",
        "task_start",
        "task_register",
        "task_start",
        "fw_set",
    ]


def test_undo_reverses_all_three_steps() -> None:
    backend = FakeBackend()
    ctx = make_ctx(backend)
    run_steps(steps_all(), ctx)
    backend.calls.clear()
    assert undo_steps(steps_all(), make_ctx(backend)) == 0
    assert mutating(backend) == ["fw_remove", "task_stop", "task_unregister", "task_stop", "task_unregister"]
    assert not backend.task_exists(LHM_TASK)
    assert not backend.task_exists(APP_TASK)
    assert backend.fw_get(RULE_NAME) is None


def test_tailscale_skips_without_executable_and_does_not_ask() -> None:
    backend = FakeBackend()
    asked: list[str] = []
    out = io.StringIO()
    fields = TailscaleStep(scripted(["y"], asked)).run(make_ctx(backend, out))
    assert fields == {"skipped": "not installed"}
    assert asked == []
    assert out.getvalue() == ""


def test_tailscale_declined_changes_nothing() -> None:
    backend = FakeBackend()
    backend.which_map["tailscale.exe"] = Path("C:/Program Files/Tailscale/tailscale.exe")
    run_steps([FirewallStep()], make_ctx(backend))
    before = backend.rules[RULE_NAME]
    fields = TailscaleStep(scripted(["n"])).run(make_ctx(backend))
    assert fields == {"skipped": "declined"}
    assert backend.rules[RULE_NAME] == before


def test_tailscale_accepted_adds_range_and_prints_serve_command() -> None:
    backend = FakeBackend()
    backend.which_map["tailscale.exe"] = Path("C:/Program Files/Tailscale/tailscale.exe")
    run_steps([FirewallStep()], make_ctx(backend))
    out = io.StringIO()
    fields = TailscaleStep(scripted(["y"])).run(make_ctx(backend, out))
    assert fields == {"range": TAILSCALE_RANGE}
    assert backend.rules[RULE_NAME].remote == f"LocalSubnet,{TAILSCALE_RANGE}"
    assert out.getvalue() == SERVE_COMMAND + "\n"
    assert SERVE_COMMAND == "tailscale serve --bg 8765"


def test_tailscale_yes_flag_skips_question() -> None:
    backend = FakeBackend()
    backend.which_map["tailscale.exe"] = Path("C:/Program Files/Tailscale/tailscale.exe")
    run_steps([FirewallStep()], make_ctx(backend))
    asked: list[str] = []
    TailscaleStep(scripted([], asked)).run(make_ctx(backend, yes=True))
    assert asked == []
    assert backend.rules[RULE_NAME].remote == f"LocalSubnet,{TAILSCALE_RANGE}"


def test_tailscale_missing_rule_raises() -> None:
    backend = FakeBackend()
    backend.which_map["tailscale.exe"] = Path("C:/Program Files/Tailscale/tailscale.exe")
    with pytest.raises(StepError, match="firewall rule not found"):
        TailscaleStep(scripted(["y"])).run(make_ctx(backend))


def test_tailscale_range_kept_on_later_firewall_done_check() -> None:
    backend = FakeBackend()
    backend.which_map["tailscale.exe"] = Path("C:/Program Files/Tailscale/tailscale.exe")
    run_steps([FirewallStep()], make_ctx(backend))
    TailscaleStep(scripted(["y"])).run(make_ctx(backend))
    assert FirewallStep().done(make_ctx(backend)) is True


def test_tailscale_done_after_recorded_run() -> None:
    backend = FakeBackend()
    step = TailscaleStep(scripted(["n"]))
    run_steps([step], make_ctx(backend))
    assert step.done(make_ctx(backend)) is True


def test_firewall_done_requires_enabled_rule() -> None:
    backend = FakeBackend()
    run_steps([FirewallStep()], make_ctx(backend))
    rule = backend.rules[RULE_NAME]
    backend.rules[RULE_NAME] = FirewallRule(rule.name, rule.port, rule.profile, rule.remote, rule.program, False)
    assert FirewallStep().done(make_ctx(backend)) is False


def test_dry_run_tailscale_without_rule_does_not_fail() -> None:
    backend = FakeBackend()
    backend.which_map["tailscale.exe"] = Path("C:/Program Files/Tailscale/tailscale.exe")
    out = io.StringIO()
    fields = TailscaleStep(scripted(["y"])).run(make_ctx(backend, out, dry_run=True))
    assert fields == {"range": TAILSCALE_RANGE}
