from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import IO

from fluentmon.setup.backend import Backend
from fluentmon.setup.detect import Detected, detect, find_cpu_hardware, to_config_fields, walk_sensors
from fluentmon.setup.pins import LHM_DATA_URL
from fluentmon.setup.runner import Context, StepError

ROWS: tuple[str, ...] = (
    "cpus",
    "power_sensor",
    "clock_sensors",
    "temp_sensor",
    "tdp_w",
    "pkg_max_w",
    "tjmax_c",
    "watch_dir",
    "runs_dir",
)
FOOTER = "[Enter] accept all   [1-9] change a value   [q] quit without changes"
SENSOR_KEYS = ("power_sensor", "temp_sensor")
NUMBER_KEYS = ("tdp_w", "pkg_max_w", "tjmax_c")


def _show(d: Detected, key: str) -> str:
    value = getattr(d, key)
    if key == "cpus":
        return "[" + ", ".join(str(cpu) for cpu in value) + "]"
    if key == "clock_sensors":
        return ", ".join(value) if value else "not found, key left out"
    if value is None:
        return "not found, key left out" if key in SENSOR_KEYS else "unknown"
    if key in NUMBER_KEYS:
        return f"{value:g}"
    return str(value)


def _screen(d: Detected, out: IO[str]) -> None:
    for index, key in enumerate(ROWS, start=1):
        out.write(f"{index}  {key:<15}{_show(d, key)}\n")
    out.write(FOOTER + "\n")


def _ask_number(ask: Callable[[str], str], out: IO[str], prompt: str) -> float:
    while True:
        try:
            answer = ask(prompt)
        except EOFError as exc:
            raise StepError(f"no answer for: {prompt.rstrip(':')}") from exc
        try:
            number = float(answer.strip())
        except ValueError:
            number = 0.0
        if number > 0:
            return number
        out.write("enter a positive number\n")


def _parse_typed(key: str, text: str, logical: int) -> object | None:
    text = text.strip()
    if key == "cpus":
        items = [item.strip() for item in text.split(",") if item.strip()]
        try:
            cpus = [int(item) for item in items]
        except ValueError:
            return None
        if not cpus or any(cpu < 0 or cpu >= logical for cpu in cpus):
            return None
        return cpus
    if key == "clock_sensors":
        return [item.strip() for item in text.split(",") if item.strip()]
    if key in NUMBER_KEYS:
        try:
            number = float(text)
        except ValueError:
            return None
        return number if number > 0 else None
    return text if text else None


def _change(d: Detected, key: str, ask: Callable[[str], str], out: IO[str], logical: int) -> Detected:
    alternatives = d.alternatives.get(key, [])
    for number, alternative in enumerate(alternatives, start=1):
        out.write(f"  {number}  {alternative}\n")
    out.write("  0  type a value\n")
    try:
        choice = ask("> ").strip()
    except EOFError:
        return d
    if choice == "":
        return d
    if choice == "0":
        while True:
            try:
                text = ask("New value: ")
            except EOFError:
                return d
            if text.strip() == "" and key == "clock_sensors":
                return replace(d, clock_sensors=[])
            value = _parse_typed(key, text, logical)
            if value is not None:
                return replace(d, **{key: value})
            out.write("invalid value\n")
    if choice.isdigit() and 1 <= int(choice) <= len(alternatives):
        return replace(d, **{key: alternatives[int(choice) - 1]})
    out.write("invalid choice\n")
    return d


def confirm(
    d: Detected,
    logical: int,
    ask: Callable[[str], str],
    out: IO[str],
    yes: bool,
) -> Detected:
    missing = d.tdp_w is None or d.tjmax_c is None
    if yes:
        if missing:
            raise StepError("tdp_w and tjmax_c are unknown for this CPU; run setup without --yes")
        _screen(d, out)
        return d

    current = d
    if current.tdp_w is None:
        current = replace(current, tdp_w=_ask_number(ask, out, "TDP in watts:"))
    if current.tjmax_c is None:
        current = replace(current, tjmax_c=_ask_number(ask, out, "Tjmax in degrees C:"))
    if current.pkg_max_w is None and current.tdp_w is not None:
        current = replace(current, pkg_max_w=float(round(1.35 * current.tdp_w)))

    while True:
        _screen(current, out)
        try:
            answer = ask("> ").strip()
        except EOFError:
            break
        if answer == "":
            break
        if answer.lower() == "q":
            raise StepError("setup cancelled")
        if not answer.isdigit() or not 1 <= int(answer) <= len(ROWS):
            out.write(f"choose a row from 1 to {len(ROWS)}, or press Enter\n")
            continue
        current = _change(current, ROWS[int(answer) - 1], ask, out, logical)
    return current


class DetectStep:
    id = "detect"
    undoable = False

    def __init__(
        self,
        ask: Callable[[str], str],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ask = ask
        self._sleep = sleep

    def done(self, ctx: Context) -> bool:
        entry = ctx.manifest.get(self.id)
        return entry is not None and isinstance(entry.get("values"), dict)

    def run(self, ctx: Context) -> dict:
        backend: Backend = ctx.backend
        topology = backend.cpu_topology()
        if ctx.dry_run:
            ctx.out.write("would detect sensors\n")
            return {"cpu_name": topology.name, "values": {}}

        tree = None
        for attempt in range(20):
            tree = backend.http_get_json(LHM_DATA_URL)
            if tree is not None:
                break
            if attempt < 19:
                self._sleep(1.0)
        if tree is None:
            raise StepError("LHM data.json is not reachable on 127.0.0.1:8085")

        hardware = find_cpu_hardware(tree, topology.name)
        if hardware is None or not any(s.path.startswith(hardware + "/") for s in walk_sensors(tree)):
            raise StepError(
                "LHM is running but reports no CPU sensors; security software may be blocking its driver"
            )

        ctx.out.write(
            f"CPU            {topology.name}, {topology.physical} cores, {topology.logical} threads\n"
        )
        found = 1 if backend.find_ansys() is not None else 0
        ctx.out.write(f"Fluent         installs found under ANSYS_INC: {found}\n")

        detected = detect(tree, topology)
        confirmed = confirm(detected, topology.logical, self._ask, ctx.out, ctx.yes)
        values = to_config_fields(confirmed)
        values["watch_dir"] = confirmed.watch_dir
        values["runs_dir"] = confirmed.runs_dir
        return {"cpu_name": topology.name, "values": values}
