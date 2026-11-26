from __future__ import annotations

from datetime import datetime, timedelta, timezone
from weakref import WeakKeyDictionary

import pytest

from fluentmon.config import Config
from fluentmon.model.session import Changes, SessionState
from fluentmon.transcript.parser import IterEv, LineEv, Parser

HEADER = "  iter  continuity  x-velocity  y-velocity  z-velocity      energy           k       omega     time/iter"
DATA = "   118  2.3156e-03  1.2034e-04  9.8812e-05  1.1102e-04  3.4410e-07  2.0981e-04  5.5521e-04  0:01:12  382"
FLOW = "Flow time = 1.2000e-02s, time step = 120"
MORE = "880 more time steps"
CONVERGED = "! 1412 solution is converged"
FIXED_TIME = datetime(2026, 11, 25, 10, 41, 7, tzinfo=timezone(timedelta(hours=9)))


def make_state(cfg: Config | None = None) -> SessionState:
    return SessionState(cfg or Config(), "s1", "duct", FIXED_TIME, clock=lambda: FIXED_TIME)


PARSERS: WeakKeyDictionary[SessionState, Parser] = WeakKeyDictionary()


def drive(state: SessionState, lines: list[str], now: float = 0.0) -> list[Changes]:
    parser = PARSERS.setdefault(state, Parser())
    out: list[Changes] = []
    for line in lines:
        for ev in parser.feed(line):
            out.append(state.apply(ev, now))
    return out


def data(it: int, left: int, eta: str = "0:00:10", values: str = "1.0e-03  5.0e-04") -> str:
    return f"{it:6d}  {values}  {eta}  {left}"


TWO_NAMES = "  iter  continuity  k  time/iter"


def events_of(changes: list[Changes]) -> list[dict]:
    result: list[dict] = []
    for change in changes:
        result.extend(change.events)
    return result


def test_section_one_sample_columns_and_bar() -> None:
    state = make_state()
    drive(state, [HEADER, DATA])
    assert state.columns == ["continuity", "x-velocity", "y-velocity", "z-velocity", "energy", "k", "omega"]
    assert state.kinds == ["res"] * 7
    assert list(state.iters) == [118]
    assert len(state.values) == 7
    bars = state.bars_msg()
    assert bars["iter"]["label"] == "Iteration 1 / 383 · 382 left · ETA 0:01:12"
    assert bars["step"] is None
    assert bars["conv"]["state"] == "ok"


def test_484_of_1000_bar() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(1, 999, "0:00:10"), data(484, 516, "0:12:40")])
    bar = state.bars_msg()["iter"]
    assert bar["p"] == pytest.approx(0.484)
    assert bar["done"] == 484
    assert bar["total"] == 1000
    assert bar["label"] == "Iteration 484 / 1000 · 516 left · ETA 0:12:40"


def test_converged_label_and_event() -> None:
    state = make_state()
    changes = drive(state, [TWO_NAMES, data(1, 999), CONVERGED])
    assert state.status == "converged"
    bar = state.bars_msg()["iter"]
    assert bar["p"] == 1.0
    assert bar["label"] == "Converged at 1412"
    converged = [ev for ev in events_of(changes) if ev["kind"] == "converged"]
    assert converged == [
        {"t": "event", "kind": "converged", "iter": 1412, "text": CONVERGED, "ts": FIXED_TIME.isoformat(timespec="seconds")}
    ]


def test_converged_bar_uses_converged_iteration() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(10, 90), CONVERGED])
    assert state.bars_msg()["iter"]["label"] == "Converged at 1412"


def test_running_idle_resumed_with_fake_time() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(1, 99)], now=0.0)
    drive(state, [data(2, 98)], now=1.0)
    assert state.status == "running"
    assert state.check_idle(10.0).events == []
    assert state.status == "running"
    idle = state.check_idle(40.0)
    assert [ev["kind"] for ev in idle.events] == ["idle"]
    assert state.status == "idle"
    changes = drive(state, [data(3, 97)], now=41.0)
    kinds = [ev["kind"] for ev in events_of(changes)]
    assert "resumed" in kinds
    assert state.status == "running"


def test_idle_threshold_uses_mean_gap() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(1, 99)], now=0.0)
    for index, now in enumerate([20.0, 40.0, 60.0], start=2):
        drive(state, [data(index, 100 - index)], now=now)
    assert state.check_idle(60.0 + 99.0).events == []
    assert [ev["kind"] for ev in state.check_idle(60.0 + 101.0).events] == ["idle"]


def test_reinitialisation_archives_and_sets_snapshot() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(1, 10), data(2, 9), data(3, 8)])
    changes = drive(state, [data(1, 10)])
    assert len(state.archived) == 1
    assert list(state.iters) == [1]
    flat = [change for change in changes if change.snapshot]
    assert flat
    kinds = [ev["kind"] for ev in events_of(changes)]
    assert kinds.count("run_start") == 1


def test_run_start_event_only_for_first_of_session() -> None:
    state = make_state()
    changes = drive(state, [TWO_NAMES, data(1, 10), data(2, 9), data(3, 20), data(4, 19)])
    starts = [ev for ev in events_of(changes) if ev["kind"] == "run_start"]
    assert len(starts) == 1


def test_first_time_step_switches_to_transient() -> None:
    state = make_state()
    drive(state, [HEADER, DATA])
    assert state.mode == "steady"
    changes = drive(state, [FLOW, MORE], now=5.0)
    assert state.mode == "transient"
    step_changes = [change for change in changes if change.snapshot]
    assert len(step_changes) == 1
    assert state.bars_msg()["step"] is not None


def test_step_bar_and_iter_per_step_label() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(1, 19)])
    drive(state, [FLOW, MORE], now=0.0)
    bars = state.bars_msg()
    assert bars["step"]["n"] == 120
    assert bars["step"]["left"] == 880
    assert bars["iter"]["label"].startswith("Iterations this step")


def test_console_numbers_are_absolute_after_overflow() -> None:
    state = make_state()
    last: Changes | None = None
    for _ in range(25_000):
        last = state.apply(LineEv("x", "n"), 0.0)
    assert len(state.console) == 20_000
    assert state.console[-1] == (25_000, "n", "x")
    assert state.console[0][0] == 5_001
    assert last is not None
    assert last.lines == [(25_000, "n", "x")]


def test_header_kinds_and_report_column() -> None:
    state = make_state()
    drive(state, ["  iter  continuity  uds-3  cd-1  time/iter", "   1  1.0e-02  2.0e-02  9.0  0:00:01  9"])
    assert state.kinds == ["res", "res", "rep"]


def test_report_column_never_enters_convergence() -> None:
    state = make_state()
    drive(state, ["  iter  continuity  cd-1  time/iter", "   1  1.0e-02  9.0  0:00:01  9"])
    bars = state.bars_msg()
    assert bars["conv"]["limiting"] == "continuity"


def test_extra_residual_names_are_residuals() -> None:
    cfg = Config()
    cfg.convergence.extra_residual_names = ["mycustom"]
    state = make_state(cfg)
    drive(state, ["  iter  continuity  mycustom  time/iter", "   1  1.0e-02  1.0e-03  0:00:01  9"])
    assert state.kinds == ["res", "res"]


def test_iteration_before_header_is_ignored() -> None:
    state = make_state()
    changes = state.apply(IterEv(1, (1.0,), 0, 5), 0.0)
    assert changes.rows == []
    assert list(state.iters) == []


def test_repeated_header_changes_nothing() -> None:
    state = make_state()
    drive(state, [TWO_NAMES])
    changes = drive(state, [TWO_NAMES])
    assert all(not change.snapshot for change in changes)


def test_problem_then_iteration_returns_to_running() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(1, 9)])
    changes = drive(state, ["Divergence detected in the solution"])
    assert state.status == "problem"
    assert [ev["kind"] for ev in events_of(changes)] == ["problem"]
    drive(state, [data(2, 8)], now=1.0)
    assert state.status == "running"


def test_end_emits_session_end() -> None:
    state = make_state()
    changes = state.end(0.0)
    assert state.status == "ended"
    assert [ev["kind"] for ev in changes.events] == ["session_end"]


def test_set_options_takes_effect_on_next_iteration() -> None:
    state = make_state()
    drive(state, [TWO_NAMES, data(1, 9, values="1.0e+00  1.0e-02")])
    before = state.bars_msg()["conv"]["of"]
    state.set_options(target=1e-3)
    drive(state, [data(2, 8, values="1.0e+00  1.0e-02")])
    after = state.bars_msg()["conv"]["of"]
    assert before == pytest.approx(6.0)
    assert after == pytest.approx(3.0)


def test_set_options_exclude() -> None:
    state = make_state()
    state.set_options(exclude=["continuity"])
    drive(state, [TWO_NAMES, data(1, 9, values="1.0e+00  1.0e-02")])
    assert state.bars_msg()["conv"]["limiting"] == "k"


def test_clock_is_used_for_timestamps() -> None:
    reads: list[int] = []

    def clock() -> datetime:
        reads.append(1)
        return FIXED_TIME

    state = SessionState(Config(), "s1", "duct", FIXED_TIME, clock=clock)
    changes = drive(state, [TWO_NAMES, data(1, 9)])
    state.end(0.0)
    assert len(reads) >= 1
    assert all(ev["ts"] == FIXED_TIME.isoformat(timespec="seconds") for ev in events_of(changes))


def test_changes_merge() -> None:
    first = Changes(rows=[(1, [1.0])], bars=False)
    second = Changes(lines=[(1, "n", "x")], bars=True, snapshot=True)
    first.merge(second)
    assert first.rows == [(1, [1.0])]
    assert first.lines == [(1, "n", "x")]
    assert first.bars and first.snapshot
