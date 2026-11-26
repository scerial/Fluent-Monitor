from __future__ import annotations

import json
from typing import Any

import httpx
import psutil
import pytest

from fluentmon.config import Config
from fluentmon.system import sampler as sampler_module
from fluentmon.system.memory import FluentTree
from fluentmon.system.power import LhmClient
from fluentmon.system.sampler import Sampler

HARDWARE = "AMD Ryzen 7 7800X3D"
POWER = f"{HARDWARE}/Powers/Package"
TEMP = f"{HARDWARE}/Temperatures/Core (Tctl/Tdie)"
CLOCKS = ["Core #1 (Effective)", "Core #3 (Effective)", "Core #5 (Effective)", "Core #7 (Effective)"]


def node(text: str, value: str = "", children: list | None = None) -> dict:
    return {"id": 0, "Text": text, "Min": "", "Value": value, "Max": "", "Children": children or []}


def amd_tree() -> dict:
    hardware = node(
        HARDWARE,
        children=[
            node("Clocks", children=[node("Core #1 (Effective)", "4950.0 MHz")]),
            node("Temperatures", children=[node("Core (Tctl/Tdie)", "68.0 °C")]),
            node("Powers", children=[node("Package", "71.4 W")]),
        ],
    )
    return node("Sensor", children=[node("DESKTOP", children=[hardware])])

SYS_KEYS = {"t", "cpu_ids", "cpu", "pkg_w", "pkg_max_w", "tdp_w", "clk_mhz", "tctl_c", "ram"}


def lhm(status: int = 200) -> LhmClient:
    body = json.dumps(amd_tree())
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    return LhmClient("http://127.0.0.1:8085", POWER, CLOCKS, TEMP, transport=transport)


def usable_cpus() -> list[int]:
    count = psutil.cpu_count() or 1
    return list(range(min(count, 4)))


def make(cfg: Config | None = None, status: int = 200, root: int | None = None) -> Sampler:
    config = cfg or Config()
    return Sampler(config, lhm(status), FluentTree(root))


def test_result_has_exact_keys_and_types() -> None:
    sampler = make()
    sampler.set_cpus(usable_cpus())
    msg = sampler()
    assert set(msg) == SYS_KEYS
    assert msg["t"] == "sys"
    assert msg["cpu_ids"] == usable_cpus()
    assert len(msg["cpu"]) == len(usable_cpus())
    assert len(msg["clk_mhz"]) == len(usable_cpus())
    assert set(msg["ram"]) == {"total", "used", "fluent"}
    assert msg["pkg_max_w"] == 162.0
    assert msg["tdp_w"] == 120.0


def test_clock_list_one_per_cpu_and_short_sensor_list() -> None:
    cfg = Config()
    cfg.meters.clock_sensors = ["Core #1 (Effective)"]
    sampler = Sampler(cfg, lhm(), FluentTree(None))
    sampler.set_cpus(usable_cpus())
    msg = sampler()
    assert msg["clk_mhz"][0] == 4950.0
    assert all(value is None for value in msg["clk_mhz"][1:])
    assert len(msg["clk_mhz"]) == len(usable_cpus())


def test_lhm_down_gives_none_fields_and_ok_false() -> None:
    sampler = make(status=500)
    sampler.set_cpus(usable_cpus())
    msg = sampler()
    assert msg["pkg_w"] is None
    assert msg["tctl_c"] is None
    assert sampler.lhm_ok is False


def test_fluent_not_running_gives_zero() -> None:
    sampler = make(root=None)
    sampler.set_cpus(usable_cpus())
    assert sampler()["ram"]["fluent"] == 0


def test_ram_values_are_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sampler_module, "ram_totals", lambda: (100, 200))

    class Huge:
        def uss(self) -> int:
            return 10_000

    cfg = Config()
    tree: Any = Huge()
    sampler = Sampler(cfg, lhm(), tree)
    sampler.set_cpus(usable_cpus())
    ram = sampler()["ram"]
    assert ram == {"total": 100, "used": 100, "fluent": 100}


def test_set_pkg_max_w_is_reflected() -> None:
    sampler = make()
    sampler.set_cpus(usable_cpus())
    sampler.set_pkg_max_w(140.0)
    assert sampler()["pkg_max_w"] == 140.0


def test_unknown_cpu_id_raises() -> None:
    sampler = make()
    sampler.set_cpus([psutil.cpu_count() + 100])
    with pytest.raises(ValueError):
        sampler()
