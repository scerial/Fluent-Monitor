from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from fluentmon.config import ServerCfg
from fluentmon.setup import tdp
from fluentmon.setup.backend import CpuTopology
from fluentmon.system.power import parse_value


@dataclass
class SensorNode:
    path: str
    text: str
    value: float | None


@dataclass
class Detected:
    cpu_name: str
    cpus: list[int]
    power_sensor: str | None
    clock_sensors: list[str]
    temp_sensor: str | None
    tdp_w: float | None
    tjmax_c: float | None
    pkg_max_w: float | None
    alternatives: dict[str, list[str]] = field(default_factory=dict)
    watch_dir: str = ServerCfg().watch_dir
    runs_dir: str = ServerCfg().runs_dir


def _text(node: dict) -> str:
    return str(node.get("Text", ""))


def _children(node: Any) -> list[dict]:
    if not isinstance(node, dict):
        return []
    children = node.get("Children")
    if not isinstance(children, list):
        return []
    return [child for child in children if isinstance(child, dict)]


def _has_value(node: dict) -> bool:
    return isinstance(node.get("Value"), str)


def _leaves(node: dict, ancestors: tuple[dict, ...]) -> Iterator[tuple[tuple[dict, ...], dict]]:
    children = _children(node)
    if children:
        for child in children:
            yield from _leaves(child, ancestors + (node,))
    elif _has_value(node):
        yield ancestors, node


def _hardware_nodes(node: dict) -> Iterator[dict]:
    if any(_has_value(grand) for child in _children(node) for grand in _children(child)):
        yield node
    for child in _children(node):
        yield from _hardware_nodes(child)


def walk_sensors(tree: dict) -> list[SensorNode]:
    nodes: list[SensorNode] = []
    for ancestors, leaf in _leaves(tree, ()):
        if len(ancestors) < 2:
            continue
        hardware = _text(ancestors[-2])
        group = _text(ancestors[-1])
        text = str(leaf["Value"])
        nodes.append(
            SensorNode(
                path=f"{hardware}/{group}/{_text(leaf)}",
                text=text,
                value=parse_value(text),
            )
        )
    return nodes


def find_cpu_hardware(tree: dict, cpu_name: str) -> str | None:
    hardware = list(_hardware_nodes(tree))
    for node in hardware:
        if _text(node) == cpu_name:
            return _text(node)
    for node in hardware:
        groups = {_text(child) for child in _children(node)}
        if "Powers" in groups and "Clocks" in groups:
            return _text(node)
    return None


def _core_indices(physical: int) -> list[int]:
    if physical < 4:
        return list(range(physical))
    return [0, physical // 4, physical // 2, 3 * physical // 4]


def core_cpus(physical: int, logical: int) -> list[int]:
    ratio = max(1, logical // physical)
    return [index * ratio for index in _core_indices(physical)]


def _group(sensors: list[SensorNode], hardware: str, group: str) -> list[SensorNode]:
    prefix = f"{hardware}/{group}/"
    found = [s for s in sensors if s.value is not None and s.path.startswith(prefix)]
    return sorted(found, key=lambda s: s.value if s.value is not None else 0.0, reverse=True)


def _vendor(name: str) -> str:
    if "AMD" in name:
        return "amd"
    if "Intel" in name:
        return "intel"
    return "other"


def detect(tree: dict, topology: CpuTopology) -> Detected:
    name = topology.name
    cpus = core_cpus(topology.physical, topology.logical)
    sensors = walk_sensors(tree)
    valid = {s.path for s in sensors if s.value is not None}
    hardware = find_cpu_hardware(tree, name)
    vendor = _vendor(name)

    power: str | None = None
    temp: str | None = None
    clocks: list[str] = []
    alternatives: dict[str, list[str]] = {"power_sensor": [], "clock_sensors": [], "temp_sensor": []}

    if hardware is not None:
        powers = _group(sensors, hardware, "Powers")
        temps = _group(sensors, hardware, "Temperatures")

        primary_power = {"amd": "Powers/Package", "intel": "Powers/CPU Package"}.get(vendor)
        if primary_power is not None and f"{hardware}/{primary_power}" in valid:
            power = f"{hardware}/{primary_power}"
        elif powers:
            power = powers[0].path
        alternatives["power_sensor"] = [s.path for s in powers if s.path != power]

        primary_temp = {"amd": "Temperatures/Core (Tctl/Tdie)", "intel": "Temperatures/CPU Package"}.get(vendor)
        if primary_temp is not None and f"{hardware}/{primary_temp}" in valid:
            temp = f"{hardware}/{primary_temp}"
        elif f"{hardware}/Temperatures/Core Max" in valid:
            temp = f"{hardware}/Temperatures/Core Max"
        elif temps:
            temp = temps[0].path
        alternatives["temp_sensor"] = [s.path for s in temps if s.path != temp]

        indices = _core_indices(topology.physical)
        effective = [f"Core #{index + 1} (Effective)" for index in indices]
        plain = [f"Core #{index + 1}" for index in indices]
        if all(f"{hardware}/Clocks/{n}" in valid for n in effective):
            clocks = effective
        elif all(f"{hardware}/Clocks/{n}" in valid for n in plain):
            clocks = plain

    row = tdp.lookup(name)
    tdp_w = row[0] if row is not None else None
    tjmax_c = row[1] if row is not None else None
    pkg_max_w = float(round(1.35 * tdp_w)) if tdp_w is not None else None

    return Detected(
        cpu_name=name,
        cpus=cpus,
        power_sensor=power,
        clock_sensors=clocks,
        temp_sensor=temp,
        tdp_w=tdp_w,
        tjmax_c=tjmax_c,
        pkg_max_w=pkg_max_w,
        alternatives=alternatives,
    )


def to_config_fields(d: Detected) -> dict:
    fields: dict[str, object] = {"cpus": list(d.cpus)}
    if d.power_sensor is not None:
        fields["power_sensor"] = d.power_sensor
    if d.clock_sensors:
        fields["clock_sensors"] = list(d.clock_sensors)
    if d.temp_sensor is not None:
        fields["temp_sensor"] = d.temp_sensor
    if d.tdp_w is not None:
        fields["tdp_w"] = d.tdp_w
    if d.tjmax_c is not None:
        fields["tjmax_c"] = d.tjmax_c
    if d.pkg_max_w is not None:
        fields["pkg_max_w"] = d.pkg_max_w
    return fields
