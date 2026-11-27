from __future__ import annotations

import re

TDP_TABLE: dict[str, tuple[float, float]] = {
    "AMD Ryzen 7 7800X3D": (120.0, 89.0),
}

_CORE_SUFFIX = re.compile(r"\s+\d+-Core Processor$")


def normalise(cpu_name: str) -> str:
    return _CORE_SUFFIX.sub("", cpu_name.strip()).strip()


def lookup(cpu_name: str) -> tuple[float, float] | None:
    return TDP_TABLE.get(normalise(cpu_name))
