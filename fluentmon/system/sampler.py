from __future__ import annotations

from collections.abc import Sequence

from fluentmon.config import Config
from fluentmon.system.cpu import cpu_loads
from fluentmon.system.memory import FluentTree, ram_totals
from fluentmon.system.power import LhmClient


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


class Sampler:
    def __init__(self, cfg: Config, lhm: LhmClient, tree: FluentTree) -> None:
        self.cfg = cfg
        self.lhm = lhm
        self.tree = tree
        self.cpus: list[int] = list(cfg.meters.cpus)
        self.pkg_max_w: float = cfg.meters.pkg_max_w

    @property
    def lhm_ok(self) -> bool:
        return self.lhm.ok

    def set_cpus(self, ids: Sequence[int]) -> None:
        self.cpus = list(ids)

    def set_pkg_max_w(self, w: float) -> None:
        self.pkg_max_w = w

    def __call__(self) -> dict:
        cpu = cpu_loads(self.cpus)
        reading = self.lhm.read()
        total, used = ram_totals()
        used = _clamp(used, 0, total)
        fluent = _clamp(self.tree.uss(), 0, used)
        clk = [reading.clk_mhz[k] if k < len(reading.clk_mhz) else None for k in range(len(self.cpus))]
        return {
            "t": "sys",
            "cpu_ids": list(self.cpus),
            "cpu": cpu,
            "pkg_w": reading.pkg_w,
            "pkg_max_w": self.pkg_max_w,
            "tdp_w": self.cfg.meters.tdp_w,
            "clk_mhz": clk,
            "tctl_c": reading.tctl_c,
            "ram": {"total": total, "used": used, "fluent": fluent},
        }
