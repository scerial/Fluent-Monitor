from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from fluentmon.setup.runner import Context, StepError
from fluentmon.setup.windows_steps import RULE_NAME

TAILSCALE_RANGE = "100.64.0.0/10"
SERVE_COMMAND = "tailscale serve --bg 8765"


class TailscaleStep:
    id = "tailscale"
    undoable = False

    def __init__(self, ask: Callable[[str], str]) -> None:
        self._ask = ask

    def done(self, ctx: Context) -> bool:
        return ctx.manifest.get(self.id) is not None

    def run(self, ctx: Context) -> dict:
        backend = ctx.backend
        if backend.which("tailscale.exe") is None:
            return {"skipped": "not installed"}
        if ctx.yes:
            answer = "y"
        else:
            try:
                answer = self._ask("Add the Tailscale range to the firewall rule? [y/N]")
            except EOFError:
                answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            return {"skipped": "declined"}
        rule = backend.fw_get(RULE_NAME)
        if rule is None and not ctx.dry_run:
            raise StepError("firewall rule not found")
        if rule is not None:
            backend.fw_set(replace(rule, remote=f"LocalSubnet,{TAILSCALE_RANGE}"))
        ctx.out.write(SERVE_COMMAND + "\n")
        return {"range": TAILSCALE_RANGE}

    def undo(self, ctx: Context, entry: dict) -> None:
        return None
