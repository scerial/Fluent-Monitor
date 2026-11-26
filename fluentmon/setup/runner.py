from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from fluentmon.setup.backend import Backend
from fluentmon.setup.manifest import Manifest


class StepError(RuntimeError):
    pass


@dataclass
class Context:
    backend: Backend
    manifest: Manifest
    out: IO[str]
    config_path: Path
    log_path: Path
    yes: bool = False
    rotate_token: bool = False
    lhm_zip: Path | None = None
    dry_run: bool = False
    purge: bool = False

    def log(self, step: str, action: str, result: str) -> None:
        self.backend.mkdir(self.log_path.parent)
        line = f"{self.backend.now().isoformat()} {step} {action} {result}\n"
        self.backend.append_text(self.log_path, line)


class Step(Protocol):
    id: str
    undoable: bool

    def done(self, ctx: Context) -> bool: ...

    def run(self, ctx: Context) -> dict: ...

    def undo(self, ctx: Context, entry: dict) -> None: ...


def run_steps(steps: Sequence[Step], ctx: Context) -> int:
    for step in steps:
        try:
            finished = step.done(ctx)
        except Exception as exc:
            ctx.log(step.id, "check", f"failed, treated as not done: {exc}")
            finished = False
        if finished:
            ctx.log(step.id, "run", "skipped, already done")
            continue
        try:
            fields = step.run(ctx)
        except Exception as exc:
            ctx.log(step.id, "run", f"failed: {exc}")
            ctx.out.write(f"step {step.id} failed: {exc}\n")
            return 1
        ctx.manifest.record(step.id, **fields)
        ctx.log(step.id, "run", "done")
    return 0


def undo_steps(steps: Sequence[Step], ctx: Context) -> int:
    recorded = {entry["id"] for entry in ctx.manifest.entries() if entry.get("done")}
    status = 0
    for step in reversed(list(steps)):
        if not step.undoable or step.id not in recorded:
            continue
        entry = ctx.manifest.get(step.id)
        if entry is None:
            continue
        try:
            step.undo(ctx, entry)
        except Exception as exc:
            ctx.log(step.id, "undo", f"failed: {exc}")
            ctx.out.write(f"undo of step {step.id} failed: {exc}\n")
            status = 1
            continue
        ctx.log(step.id, "undo", "done")
        ctx.manifest.remove(step.id)
    return status
