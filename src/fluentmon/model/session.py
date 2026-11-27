from __future__ import annotations

import logging
import re
from array import array
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from fluentmon.config import Config
from fluentmon.model.decimate import decimate
from fluentmon.model.progress import ConvTracker, StepTracker, iter_bar
from fluentmon.transcript.parser import (
    ConvergedEv,
    Event,
    HeaderEv,
    IterEv,
    LineEv,
    ProblemEv,
    RunStartEv,
    StepEv,
)

log = logging.getLogger("fluentmon.model.session")

RESIDUAL_NAMES = frozenset(
    {
        "continuity",
        "x-velocity",
        "y-velocity",
        "z-velocity",
        "energy",
        "k",
        "epsilon",
        "omega",
        "nut",
        "intermit",
        "retheta",
        "v2",
    }
)
UDS_NAME = re.compile(r"uds-\d+")
CONSOLE_MAX = 20_000
IDLE_FLOOR_S = 30.0
IDLE_FACTOR = 5.0
GAP_WINDOW = 50


def _default_clock() -> datetime:
    return datetime.now().astimezone()


@dataclass
class Changes:
    rows: list[tuple[int, list[float]]] = field(default_factory=list)
    lines: list[tuple[int, str, str]] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    bars: bool = False
    snapshot: bool = False

    def merge(self, other: Changes) -> None:
        self.rows.extend(other.rows)
        self.lines.extend(other.lines)
        self.events.extend(other.events)
        self.bars = self.bars or other.bars
        self.snapshot = self.snapshot or other.snapshot


class SessionState:
    def __init__(
        self,
        cfg: Config,
        session_id: str,
        case: str,
        started: datetime,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.session_id = session_id
        self.case = case
        self.started = started
        self.clock: Callable[[], datetime] = clock or _default_clock
        self.mode = "steady"
        self.status = "running"
        self.columns: list[str] = []
        self.kinds: list[str] = []
        self.iters: array = array("q")
        self.values: list[array] = []
        self.console: deque[tuple[int, str, str]] = deque(maxlen=CONSOLE_MAX)
        self.line_count = 0
        self.archived: list[tuple[list[str], list[str], array, list[array]]] = []
        self.i0: int | None = None
        self.conv = ConvTracker(cfg.convergence.target, cfg.convergence.exclude)
        self.step = StepTracker()
        self._conv: dict[str, Any] | None = None
        self._step: dict[str, Any] | None = None
        self._last_it: int | None = None
        self._left = 0
        self._eta_s: int | None = None
        self._converged_at: int | None = None
        self._emit_run_start = True
        self._times: deque[float] = deque(maxlen=GAP_WINDOW + 1)

    def apply(self, ev: Event, now: float) -> Changes:
        if isinstance(ev, LineEv):
            return self._line(ev)
        if isinstance(ev, HeaderEv):
            return self._header(ev)
        if isinstance(ev, IterEv):
            return self._iter(ev, now)
        if isinstance(ev, RunStartEv):
            return self._run_start(ev)
        if isinstance(ev, StepEv):
            return self._step_ev(ev, now)
        if isinstance(ev, ConvergedEv):
            return self._converged(ev)
        if isinstance(ev, ProblemEv):
            return self._problem(ev)
        return Changes()

    def check_idle(self, now: float) -> Changes:
        changes = Changes()
        if self.status not in ("running", "problem") or not self._times:
            return changes
        gaps = [later - earlier for earlier, later in pairwise(self._times)]
        mean_gap = sum(gaps) / len(gaps) if gaps else 0.0
        threshold = max(IDLE_FLOOR_S, IDLE_FACTOR * mean_gap)
        if now - self._times[-1] > threshold:
            self.status = "idle"
            changes.events.append(self._event("idle", "no iterations received", self._last_iter()))
        return changes

    def end(self, now: float) -> Changes:
        self.status = "ended"
        changes = Changes()
        changes.events.append(self._event("session_end", "session ended", self._last_iter()))
        return changes

    def bars_msg(self) -> dict[str, Any]:
        iter_part = None
        converged = self._converged_at is not None
        i = self._converged_at if converged else self._last_it
        if len(self.iters) and i is not None:
            i0 = self.i0 if self.i0 is not None else self.iters[0]
            iter_part = iter_bar(i0, i, self._left, self._eta_s, converged, self.mode == "transient")
        return {"t": "bars", "iter": iter_part, "conv": self._conv, "step": self._step}

    def snapshot_msg(self, tail: int = 500, max_points: int = 1500) -> dict[str, Any]:
        n = len(self.iters)
        cut = max(0, n - tail)
        old_columns = [column[:cut] for column in self.values]
        old_indices = decimate(self.iters[:cut], old_columns, max_points)
        indices = old_indices + list(range(cut, n))
        console_tail = list(self.console)[-500:]
        first = console_tail[0][0] if console_tail else self.line_count + 1
        return {
            "t": "snapshot",
            "session": {
                "id": self.session_id,
                "case": self.case,
                "mode": self.mode,
                "status": self.status,
                "started": self.started.isoformat(timespec="seconds"),
            },
            "columns": list(self.columns),
            "kinds": list(self.kinds),
            "iters": [self.iters[i] for i in indices],
            "values": [[column[i] for i in indices] for column in self.values],
            "bars": {key: value for key, value in self.bars_msg().items() if key != "t"},
            "con_tail": {
                "first": first,
                "lines": [text for _, _, text in console_tail],
                "kinds": "".join(kind for _, kind, _ in console_tail),
            },
        }

    def console_range(self, before: int, n: int) -> dict[str, Any] | None:
        start = max(1, before - n)
        oldest = self.console[0][0] if self.console else self.line_count + 1
        if start < oldest:
            return None
        entries = [entry for entry in self.console if start <= entry[0] < before]
        return {
            "t": "con",
            "first": start,
            "lines": [text for _, _, text in entries],
            "kinds": "".join(kind for _, kind, _ in entries),
        }

    def set_options(self, target: float | None = None, exclude: Sequence[str] | None = None) -> None:
        if target is not None:
            self.conv.target = target
        if exclude is not None:
            self.conv.exclude = set(exclude)

    def _kind_of(self, name: str) -> str:
        if (
            name in RESIDUAL_NAMES
            or UDS_NAME.fullmatch(name) is not None
            or name in self.cfg.convergence.extra_residual_names
        ):
            return "res"
        return "rep"

    def _ref_window(self) -> int:
        return 1 if self.mode == "transient" else 5

    def _start_series(self, names: list[str]) -> None:
        self.columns = list(names)
        self.kinds = [self._kind_of(name) for name in names]
        self.iters = array("q")
        self.values = [array("d") for _ in names]

    def _archive(self) -> None:
        if len(self.iters):
            self.archived.append((self.columns, self.kinds, self.iters, self.values))

    def _reset_trackers(self) -> None:
        self.conv.reset(self._ref_window())
        self.step.reset()
        self._conv = None
        self._step = None

    def _last_iter(self) -> int | None:
        return self._last_it

    def _event(self, kind: str, text: str, it: int | None) -> dict:
        return {
            "t": "event",
            "kind": kind,
            "iter": it,
            "text": text,
            "ts": self.clock().isoformat(timespec="seconds"),
        }

    def _line(self, ev: LineEv) -> Changes:
        self.line_count += 1
        entry = (self.line_count, ev.kind, ev.text)
        self.console.append(entry)
        changes = Changes()
        changes.lines.append(entry)
        return changes

    def _header(self, ev: HeaderEv) -> Changes:
        if tuple(self.columns) == ev.names:
            return Changes()
        self._archive()
        self._start_series(list(ev.names))
        self.i0 = None
        self._reset_trackers()
        return Changes(snapshot=True)

    def _iter(self, ev: IterEv, now: float) -> Changes:
        changes = Changes()
        if not self.columns:
            log.warning("iteration %d before any header ignored", ev.it)
            return changes
        if len(ev.values) != len(self.columns):
            log.warning("iteration %d has %d values for %d columns", ev.it, len(ev.values), len(self.columns))
            return changes
        if len(self.iters) and ev.it < self.iters[-1]:
            self._archive()
            self._start_series(self.columns)
            self.i0 = ev.it
            self._reset_trackers()
            changes.snapshot = True
            self._emit_run_start = False
            changes.events.append(self._event("run_start", "run restarted", ev.it))
        if self.i0 is None:
            self.i0 = ev.it
        if self.status == "idle":
            changes.events.append(self._event("resumed", "iterations resumed", ev.it))
            self._emit_run_start = True
        self.status = "running"
        self.iters.append(ev.it)
        for column, value in zip(self.values, ev.values):
            column.append(value)
        residuals = {
            name: value
            for name, kind, value in zip(self.columns, self.kinds, ev.values)
            if kind == "res"
        }
        self._conv = self.conv.update(ev.it, residuals)
        self._last_it = ev.it
        self._left = ev.left
        self._eta_s = ev.eta_s
        self._converged_at = None
        self._times.append(now)
        changes.rows.append((ev.it, list(ev.values)))
        changes.bars = True
        return changes

    def _run_start(self, ev: RunStartEv) -> Changes:
        self.i0 = ev.it
        self.conv.reset(self._ref_window())
        self._conv = None
        changes = Changes()
        if self._emit_run_start:
            self._emit_run_start = False
            changes.events.append(self._event("run_start", "run started", ev.it))
        return changes

    def _step_ev(self, ev: StepEv, now: float) -> Changes:
        changes = Changes(bars=True)
        if self.mode == "steady":
            self.mode = "transient"
            changes.snapshot = True
        self._step = self.step.update(now, ev.step, ev.flow_time, ev.more)
        return changes

    def _converged(self, ev: ConvergedEv) -> Changes:
        self.status = "converged"
        self._converged_at = ev.it
        changes = Changes(bars=True)
        changes.events.append(self._event("converged", ev.text, ev.it))
        return changes

    def _problem(self, ev: ProblemEv) -> Changes:
        was_problem = self.status == "problem"
        self.status = "problem"
        changes = Changes()
        if not was_problem:
            changes.events.append(self._event("problem", ev.text, self._last_iter()))
        return changes
