from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from fluentmon.transcript.assembler import LineAssembler
from fluentmon.transcript.parser import (
    ConvergedEv,
    HeaderEv,
    IterEv,
    LineEv,
    Parser,
    ProblemEv,
    RunStartEv,
    StepEv,
    num,
)

STEADY_HEADER = "  iter  continuity  x-velocity  y-velocity  z-velocity      energy           k       omega     time/iter"
STEADY_DATA = "   118  2.3156e-03  1.2034e-04  9.8812e-05  1.1102e-04  3.4410e-07  2.0981e-04  5.5521e-04  0:01:12  382"
FLOW = "Flow time = 1.2000e-02s, time step = 120"
MORE = "880 more time steps"
CONVERGED_LINE = "! 1412 solution is converged"

NAMES2 = "  iter  continuity  k  time/iter"


def data_line(it: int, values: list[str], eta: str, left: int) -> str:
    return f"{it:6d}  " + "  ".join(values) + f"  {eta}  {left}"


def run(lines: list[str]) -> list[object]:
    parser = Parser()
    events: list[object] = []
    for line in lines:
        events.extend(parser.feed(line))
    return events


def test_steady_sample_lines() -> None:
    events = run([STEADY_HEADER, STEADY_DATA])
    assert events[0] == LineEv(STEADY_HEADER, "h")
    assert events[1] == HeaderEv(
        ("continuity", "x-velocity", "y-velocity", "z-velocity", "energy", "k", "omega")
    )
    assert events[2] == LineEv(STEADY_DATA, "r")
    assert events[3] == RunStartEv(118, 382)
    iter_ev = events[4]
    assert isinstance(iter_ev, IterEv)
    assert iter_ev.it == 118
    assert iter_ev.eta_s == 72
    assert iter_ev.left == 382
    assert len(iter_ev.values) == 7
    assert iter_ev.values[0] == 2.3156e-03
    assert len(events) == 5


def test_transient_pair_gives_step() -> None:
    events = run([FLOW, MORE])
    assert events[0] == LineEv(FLOW, "s")
    assert events[1] == LineEv(MORE, "s")
    assert events[2] == StepEv(0.012, 120, 880)
    assert len(events) == 3


def test_more_steps_without_flow_is_plain_line_kind_n() -> None:
    events = run([MORE])
    assert events == [LineEv(MORE, "n")]


def test_run_start_only_when_left_grows() -> None:
    lines = [
        STEADY_HEADER,
        data_line(1, ["1.0", "0.5", "0.2", "0.1", "0.1", "0.1", "0.1"], "0:00:10", 100),
        data_line(2, ["0.9", "0.5", "0.2", "0.1", "0.1", "0.1", "0.1"], "0:00:09", 99),
        data_line(3, ["0.8", "0.5", "0.2", "0.1", "0.1", "0.1", "0.1"], "0:00:08", 150),
    ]
    events = run(lines)
    starts = [ev for ev in events if isinstance(ev, RunStartEv)]
    assert starts == [RunStartEv(1, 100), RunStartEv(3, 150)]


def test_mismatched_data_line_gives_only_kind_r() -> None:
    parser = Parser()
    parser.feed(NAMES2)
    events = parser.feed("   5  1.0  0.5  0.2  0:00:01  3")
    assert events == [LineEv("   5  1.0  0.5  0.2  0:00:01  3", "r")]


def test_repeated_header_emits_once() -> None:
    events = run([NAMES2, NAMES2])
    headers = [ev for ev in events if isinstance(ev, HeaderEv)]
    assert headers == [HeaderEv(("continuity", "k"))]


def test_nan_tokens_become_nan() -> None:
    parser = Parser()
    parser.feed(NAMES2)
    events = parser.feed("   7  nan  -nan(ind)  0:00:01  9")
    iter_ev = events[-1]
    assert isinstance(iter_ev, IterEv)
    assert math.isnan(iter_ev.values[0])
    assert math.isnan(iter_ev.values[1])


def test_inf_and_qnan_tokens_become_nan() -> None:
    assert math.isnan(num("inf"))
    assert math.isnan(num("-inf"))
    assert math.isnan(num("1.#QNAN"))
    assert math.isnan(num("-nan(ind)"))
    assert num("2.5") == 2.5


def test_converged_line() -> None:
    events = run([CONVERGED_LINE])
    assert events == [LineEv(CONVERGED_LINE, "n"), ConvergedEv(1412, CONVERGED_LINE)]


def test_divergence_is_error_and_problem() -> None:
    line = "Divergence detected in the solution"
    events = run([line])
    assert events == [LineEv(line, "e"), ProblemEv(line)]


def test_warning_line_kind_w() -> None:
    line = "Warning: reversed flow in 12 cells"
    events = run([line])
    assert events[0] == LineEv(line, "w")
    assert events[1] == ProblemEv(line)


def test_trailing_cr_and_spaces_are_ignored() -> None:
    parser = Parser()
    parser.feed(NAMES2)
    events = parser.feed("   9  1.0  0.5  0:00:01  4\r")
    assert events[0] == LineEv("   9  1.0  0.5  0:00:01  4", "r")
    assert isinstance(events[-1], IterEv)
    assert events[-1].left == 4


def test_leading_and_trailing_spaces_on_header() -> None:
    parser = Parser()
    events = parser.feed("   " + NAMES2.strip() + "   ")
    assert events[1] == HeaderEv(("continuity", "k"))


def build_transcript() -> str:
    lines = [
        STEADY_HEADER,
        STEADY_DATA,
        data_line(119, ["2.0e-03", "1.0e-04", "9.0e-05", "1.0e-04", "3.0e-07", "2.0e-04", "5.0e-04"], "0:01:11", 381),
        FLOW,
        MORE,
        "Warning: check the mesh",
        CONVERGED_LINE,
        "",
    ]
    return "\r\n".join(lines[:4]) + "\n" + "\n".join(lines[4:])


TRANSCRIPT = build_transcript()


def events_of(chunks: list[bytes]) -> list[object]:
    assembler = LineAssembler()
    parser = Parser()
    events: list[object] = []
    for chunk in chunks:
        for line in assembler.feed(chunk):
            events.extend(parser.feed(line))
    return events


@settings(max_examples=100, deadline=None)
@given(cuts=st.lists(st.integers(min_value=0, max_value=len(TRANSCRIPT.encode("utf-8"))), max_size=60))
def test_random_chunks_give_same_events(cuts: list[int]) -> None:
    data = TRANSCRIPT.encode("utf-8")
    points = sorted(set(c for c in cuts if 0 < c < len(data))) + [len(data)]
    chunks: list[bytes] = []
    previous = 0
    for point in points:
        chunks.append(data[previous:point])
        previous = point
    assert events_of(chunks) == events_of([data])
