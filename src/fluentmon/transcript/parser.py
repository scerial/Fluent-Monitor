from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

log = logging.getLogger("fluentmon.transcript.parser")

HEADER    = re.compile(r"^\s*iter\s+(?P<names>.+?)\s+time/iter\s*$")
DATA      = re.compile(r"^\s*(?P<it>\d+)\s+(?P<vals>(?:\S+\s+)+?)"
                       r"(?P<eta>\d+:\d{2}:\d{2})\s+(?P<left>\d+)\s*$")
FLOWTIME  = re.compile(r"^\s*Flow time\s*=\s*(?P<t>[-+0-9.eE]+)\s*s?\s*,\s*time step\s*=\s*(?P<n>\d+)")
MORESTEPS = re.compile(r"^\s*(?P<n>\d+)\s+more time steps?")
CONVERGED = re.compile(r"^\s*!\s*(?P<it>\d+)\s+solution is converged")
ERROR     = re.compile(r"(Divergence detected|Floating point exception|Error:)", re.I)
WARNING   = re.compile(r"(reversed flow|turbulent viscosity limited|temperature limited)", re.I)


def num(tok: str) -> float:
    try:
        value = float(tok)
    except ValueError:
        return math.nan
    if math.isinf(value):
        return math.nan
    return value


def hms_to_seconds(text: str) -> int:
    hours, minutes, seconds = (int(part) for part in text.split(":"))
    return hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True, slots=True)
class HeaderEv:
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IterEv:
    it: int
    values: tuple[float, ...]
    eta_s: int
    left: int


@dataclass(frozen=True, slots=True)
class RunStartEv:
    it: int
    left: int


@dataclass(frozen=True, slots=True)
class StepEv:
    flow_time: float
    step: int
    more: int


@dataclass(frozen=True, slots=True)
class ConvergedEv:
    it: int
    text: str


@dataclass(frozen=True, slots=True)
class ProblemEv:
    text: str


@dataclass(frozen=True, slots=True)
class LineEv:
    text: str
    kind: str


Event = HeaderEv | IterEv | RunStartEv | StepEv | ConvergedEv | ProblemEv | LineEv


class Parser:
    def __init__(self) -> None:
        self.names: tuple[str, ...] = ()
        self._flow: tuple[float, int] | None = None
        self._last_left: int | None = None

    def feed(self, line: str) -> list[Event]:
        text = line.rstrip("\r")
        events: list[Event] = [LineEv(text, self._kind(text))]

        header = HEADER.match(text)
        if header is not None:
            names = tuple(header.group("names").split())
            if names != self.names:
                self.names = names
                events.append(HeaderEv(names))

        data = DATA.match(text)
        if data is not None:
            tokens = data.group("vals").split()
            if len(tokens) != len(self.names):
                log.warning("data line dropped: %d values for %d names", len(tokens), len(self.names))
            else:
                it = int(data.group("it"))
                left = int(data.group("left"))
                if self._last_left is None or left > self._last_left:
                    events.append(RunStartEv(it, left))
                self._last_left = left
                events.append(
                    IterEv(
                        it,
                        tuple(num(token) for token in tokens),
                        hms_to_seconds(data.group("eta")),
                        left,
                    )
                )

        flow = FLOWTIME.match(text)
        if flow is not None:
            self._flow = (num(flow.group("t")), int(flow.group("n")))

        more = MORESTEPS.match(text)
        if more is not None and self._flow is not None:
            flow_time, step = self._flow
            events.append(StepEv(flow_time, step, int(more.group("n"))))

        converged = CONVERGED.match(text)
        if converged is not None:
            events.append(ConvergedEv(int(converged.group("it")), text.strip()))

        if ERROR.search(text) is not None:
            events.append(ProblemEv(text.strip()))

        return events

    def _kind(self, text: str) -> str:
        if HEADER.match(text):
            return "h"
        if DATA.match(text):
            return "r"
        if FLOWTIME.match(text):
            return "s"
        if MORESTEPS.match(text):
            return "s" if self._flow is not None else "n"
        if ERROR.search(text):
            return "e"
        if WARNING.search(text) or text.lstrip().startswith("Warning"):
            return "w"
        return "n"
