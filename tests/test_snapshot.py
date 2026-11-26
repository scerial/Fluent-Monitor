from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from fluentmon.config import Config
from fluentmon.model.session import SessionState
from fluentmon.transcript.parser import LineEv, Parser

HEADER = "  iter  continuity  x-velocity  k  time/iter"
FIXED = datetime(2026, 11, 25, 10, 41, 7, tzinfo=timezone(timedelta(hours=9)))


TEN_HEADER = "  iter  c1  c2  c3  c4  c5  c6  c7  c8  c9  c10  time/iter"


def make_state_with(header: str) -> SessionState:
    state = SessionState(Config(), "20261125-1041_duct", "duct_kw", FIXED, clock=lambda: FIXED)
    for ev in Parser().feed(header):
        state.apply(ev, 0.0)
    return state


def make_state() -> SessionState:
    return make_state_with(HEADER)


def bare_state() -> SessionState:
    return SessionState(Config(), "s", "c", FIXED, clock=lambda: FIXED)


def filled_state(n: int, header: str = HEADER) -> SessionState:
    state = make_state_with(header)
    state.iters.extend(range(1, n + 1))
    for index, column in enumerate(state.values):
        column.extend(float(i % 97) + index for i in range(n))
    return state


def test_snapshot_before_any_header_is_empty() -> None:
    state = SessionState(Config(), "s", "c", FIXED, clock=lambda: FIXED)
    msg = state.snapshot_msg()
    assert msg["t"] == "snapshot"
    assert msg["iters"] == []
    assert msg["values"] == []
    assert msg["con_tail"] == {"first": 1, "lines": [], "kinds": ""}
    assert msg["bars"]["iter"] is None


def test_snapshot_shape_and_session_block() -> None:
    state = filled_state(10)
    msg = state.snapshot_msg()
    assert msg["session"] == {
        "id": "20261125-1041_duct",
        "case": "duct_kw",
        "mode": "steady",
        "status": "running",
        "started": FIXED.isoformat(timespec="seconds"),
    }
    assert msg["columns"] == ["continuity", "x-velocity", "k"]
    assert msg["kinds"] == ["res", "res", "res"]
    assert len(msg["values"]) == 3
    assert all(len(column) == 10 for column in msg["values"])
    assert "t" not in msg["bars"]


def test_long_series_snapshot_is_bounded_and_fast() -> None:
    state = filled_state(50_000, TEN_HEADER)
    spike_index = 20_000
    state.values[0][spike_index] = 1e9
    started = time.perf_counter()
    msg = state.snapshot_msg()
    elapsed = time.perf_counter() - started
    print(f"snapshot of 50,000 iterations x 10 columns took {elapsed:.3f} s")
    assert len(msg["iters"]) <= 2000
    assert all(a < b for a, b in zip(msg["iters"], msg["iters"][1:]))
    assert (spike_index + 1) in msg["iters"]
    assert msg["iters"][-1] == 50_000
    assert elapsed < 0.5


def test_console_tail_and_range() -> None:
    state = bare_state()
    for index in range(600):
        state.apply(LineEv(f"line {index + 1}", "n"), 0.0)
    msg = state.snapshot_msg()
    assert msg["con_tail"]["first"] == 101
    assert len(msg["con_tail"]["lines"]) == 500
    assert msg["con_tail"]["lines"][-1] == "line 600"
    assert len(msg["con_tail"]["kinds"]) == 500


def test_console_range_returns_requested_lines() -> None:
    state = bare_state()
    for index in range(100):
        state.apply(LineEv(f"line {index + 1}", "r" if index % 2 else "n"), 0.0)
    msg = state.console_range(before=51, n=10)
    assert msg == {
        "t": "con",
        "first": 41,
        "lines": [f"line {number}" for number in range(41, 51)],
        "kinds": "nr" * 5,
    }


def test_console_range_none_below_deque_start() -> None:
    state = bare_state()
    for index in range(25_000):
        state.apply(LineEv("x", "n"), 0.0)
    assert state.console_range(before=4000, n=10) is None
    assert state.console_range(before=25_001, n=10)["first"] == 24_991


def test_console_range_at_start_of_transcript() -> None:
    state = bare_state()
    for index in range(5):
        state.apply(LineEv(f"l{index + 1}", "n"), 0.0)
    msg = state.console_range(before=3, n=10)
    assert msg["first"] == 1
    assert msg["lines"] == ["l1", "l2"]
