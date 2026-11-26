from __future__ import annotations

from collections.abc import Sequence

import psutil


def prime() -> None:
    psutil.cpu_percent(percpu=True, interval=None)


def cpu_loads(ids: Sequence[int]) -> list[float]:
    values = psutil.cpu_percent(percpu=True, interval=None)
    loads: list[float] = []
    for cpu_id in ids:
        if cpu_id < 0 or cpu_id >= len(values):
            raise ValueError(f"CPU {cpu_id} is not present on this machine")
        loads.append(values[cpu_id])
    return loads
