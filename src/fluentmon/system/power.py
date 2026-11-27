from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("fluentmon.system.power")

VALUE = re.compile(r"^([-+]?\d+(?:[.,]\d+)?)\s*(MHz|GHz|W|°C|C)?$")


@dataclass
class LhmReading:
    pkg_w: float | None
    clk_mhz: list[float | None]
    tctl_c: float | None


def parse_value(text: str) -> float | None:
    match = VALUE.match(text.strip())
    if match is None:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2)
    if unit == "GHz":
        return number * 1000.0
    return number


def _children(node: Any) -> list[Any]:
    if not isinstance(node, dict):
        return []
    children = node.get("Children")
    return children if isinstance(children, list) else []


def _dfs(node: Any) -> Iterator[dict]:
    if not isinstance(node, dict):
        return
    yield node
    for child in _children(node):
        yield from _dfs(child)


def _match(node: dict, parts: list[str]) -> dict | None:
    if not parts:
        return node
    for end in range(len(parts), 0, -1):
        name = "/".join(parts[:end])
        for child in _children(node):
            if isinstance(child, dict) and child.get("Text") == name:
                found = _match(child, parts[end:])
                if found is not None:
                    return found
    return None


def find_sensor(tree: dict, path: str) -> float | None:
    parts = path.split("/")
    for node in _dfs(tree):
        text = node.get("Text")
        for end in range(1, len(parts) + 1):
            if text == "/".join(parts[:end]):
                target = _match(node, parts[end:])
                if target is not None:
                    value = target.get("Value")
                    return parse_value(value) if isinstance(value, str) else None
    return None


class LhmClient:
    def __init__(
        self,
        base_url: str,
        power_sensor: str | None,
        clock_sensors: Sequence[str],
        temp_sensor: str | None,
        timeout_s: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.power_sensor = power_sensor
        self.clock_sensors = list(clock_sensors)
        self.temp_sensor = temp_sensor
        self.ok = False
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)
        reference = power_sensor or temp_sensor
        self._hardware = reference.split("/")[0] if reference else None

    def read(self) -> LhmReading:
        empty = LhmReading(None, [None] * len(self.clock_sensors), None)
        try:
            response = self._client.get("/data.json")
            response.raise_for_status()
            tree = response.json()
        except (httpx.HTTPError, ValueError):
            log.debug("LibreHardwareMonitor unavailable")
            self.ok = False
            return empty
        if not isinstance(tree, dict):
            self.ok = False
            return empty
        self.ok = True
        pkg_w = find_sensor(tree, self.power_sensor) if self.power_sensor else None
        tctl_c = find_sensor(tree, self.temp_sensor) if self.temp_sensor else None
        clocks: list[float | None] = []
        for name in self.clock_sensors:
            if self._hardware is None:
                clocks.append(None)
            else:
                clocks.append(find_sensor(tree, f"{self._hardware}/Clocks/{name}"))
        return LhmReading(pkg_w, clocks, tctl_c)
