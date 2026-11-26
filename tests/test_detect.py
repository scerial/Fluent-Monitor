from pathlib import Path

import pytest

from fluentmon.setup.backend import CpuTopology
from fluentmon.setup.detect import (
    core_cpus,
    detect,
    find_cpu_hardware,
    to_config_fields,
    walk_sensors,
)
from fluentmon.setup.tdp import lookup
from fluentmon.system.power import find_sensor
from support import hardware_node, make_machine, make_tree

AMD_NAME = "AMD Ryzen 7 7800X3D"
INTEL_NAME = "Intel Core i7-12700K"
AMD_TOPOLOGY = CpuTopology(AMD_NAME, 8, 16)

AMD_SENSORS = {
    "Powers": {"Package": "42.1 W", "Cores": "30 W"},
    "Clocks": {
        "Core #1 (Effective)": "4500 MHz",
        "Core #3 (Effective)": "4450 MHz",
        "Core #5 (Effective)": "4400 MHz",
        "Core #7 (Effective)": "4600 MHz",
    },
    "Temperatures": {
        "Core (Tctl/Tdie)": "55.0 °C",
        "Core Max": "70 °C",
        "CCD1 (Tdie)": "61 °C",
    },
}

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("physical", "logical", "expected"),
    [
        (2, 4, [0, 2]),
        (6, 12, [0, 2, 6, 8]),
        (8, 16, [0, 4, 8, 12]),
        (16, 32, [0, 8, 16, 24]),
        (4, 4, [0, 1, 2, 3]),
        (3, 3, [0, 1, 2]),
        (6, 13, [0, 2, 6, 8]),
    ],
)
def test_core_cpus(physical: int, logical: int, expected: list[int]) -> None:
    assert core_cpus(physical, logical) == expected


def test_section_5_sample_values_on_amd_tree() -> None:
    d = detect(make_tree(AMD_NAME, AMD_SENSORS), AMD_TOPOLOGY)
    assert d.cpus == [0, 4, 8, 12]
    assert d.clock_sensors == [
        "Core #1 (Effective)",
        "Core #3 (Effective)",
        "Core #5 (Effective)",
        "Core #7 (Effective)",
    ]
    assert d.power_sensor == f"{AMD_NAME}/Powers/Package"
    assert d.temp_sensor == f"{AMD_NAME}/Temperatures/Core (Tctl/Tdie)"
    assert d.tdp_w == 120.0
    assert d.pkg_max_w == 162.0
    assert d.tjmax_c == 89.0
    assert d.alternatives["power_sensor"] == [f"{AMD_NAME}/Powers/Cores"]
    assert d.alternatives["temp_sensor"] == [
        f"{AMD_NAME}/Temperatures/Core Max",
        f"{AMD_NAME}/Temperatures/CCD1 (Tdie)",
    ]
    assert d.alternatives["clock_sensors"] == []


def test_to_config_fields_has_section_5_keys() -> None:
    fields = to_config_fields(detect(make_tree(AMD_NAME, AMD_SENSORS), AMD_TOPOLOGY))
    assert fields == {
        "cpus": [0, 4, 8, 12],
        "power_sensor": f"{AMD_NAME}/Powers/Package",
        "clock_sensors": [
            "Core #1 (Effective)",
            "Core #3 (Effective)",
            "Core #5 (Effective)",
            "Core #7 (Effective)",
        ],
        "temp_sensor": f"{AMD_NAME}/Temperatures/Core (Tctl/Tdie)",
        "tdp_w": 120.0,
        "tjmax_c": 89.0,
        "pkg_max_w": 162.0,
    }


def test_walk_sensors_paths_match_find_sensor() -> None:
    tree = make_tree(AMD_NAME, AMD_SENSORS)
    nodes = walk_sensors(tree)
    assert len(nodes) == 9
    for node in nodes:
        assert find_sensor(tree, node.path) == node.value


def test_walk_sensors_value_text_kept_and_dash_is_none() -> None:
    tree = make_tree(AMD_NAME, {"Powers": {"Package": "-"}})
    nodes = walk_sensors(tree)
    assert nodes[0].path == f"{AMD_NAME}/Powers/Package"
    assert nodes[0].text == "-"
    assert nodes[0].value is None


def test_inline_intel_tree_uses_intel_paths() -> None:
    sensors = {
        "Powers": {"CPU Package": "65 W", "IA Cores": "50 W"},
        "Clocks": {"Core #1 (Effective)": "3600 MHz", "Core #2 (Effective)": "3500 MHz",
                   "Core #3 (Effective)": "3400 MHz", "Core #4 (Effective)": "3300 MHz"},
        "Temperatures": {"CPU Package": "60 °C", "Core Max": "75 °C"},
    }
    d = detect(make_tree(INTEL_NAME, sensors), CpuTopology(INTEL_NAME, 4, 8))
    assert d.power_sensor == f"{INTEL_NAME}/Powers/CPU Package"
    assert d.temp_sensor == f"{INTEL_NAME}/Temperatures/CPU Package"
    assert d.clock_sensors == [
        "Core #1 (Effective)",
        "Core #2 (Effective)",
        "Core #3 (Effective)",
        "Core #4 (Effective)",
    ]
    assert d.cpus == [0, 2, 4, 6]
    assert d.tdp_w is None and d.tjmax_c is None and d.pkg_max_w is None


def test_clock_fallback_to_plain_core_names() -> None:
    sensors = {
        "Powers": {"Package": "42 W"},
        "Clocks": {"Core #1": "4500 MHz", "Core #3": "4450 MHz", "Core #5": "4400 MHz", "Core #7": "4600 MHz"},
    }
    d = detect(make_tree(AMD_NAME, sensors), AMD_TOPOLOGY)
    assert d.clock_sensors == ["Core #1", "Core #3", "Core #5", "Core #7"]


def test_partial_clock_match_leaves_clocks_empty() -> None:
    sensors = {
        "Powers": {"Package": "42 W"},
        "Clocks": {"Core #1 (Effective)": "4500 MHz", "Core #3 (Effective)": "4450 MHz"},
    }
    d = detect(make_tree(AMD_NAME, sensors), AMD_TOPOLOGY)
    assert d.clock_sensors == []
    assert "clock_sensors" not in to_config_fields(d)


def test_no_clocks_gives_empty_list_and_no_config_key() -> None:
    d = detect(make_tree(AMD_NAME, {"Powers": {"Package": "42 W"}}), AMD_TOPOLOGY)
    assert d.clock_sensors == []
    assert "clock_sensors" not in to_config_fields(d)


def test_power_falls_back_to_highest_powers_sensor() -> None:
    d = detect(make_tree(AMD_NAME, {"Powers": {"Cores": "30 W", "SoC": "12 W"}}), AMD_TOPOLOGY)
    assert d.power_sensor == f"{AMD_NAME}/Powers/Cores"
    assert d.alternatives["power_sensor"] == [f"{AMD_NAME}/Powers/SoC"]


def test_temp_falls_back_to_core_max_then_hottest() -> None:
    d = detect(make_tree(AMD_NAME, {"Temperatures": {"Core Max": "70 °C", "Other": "40 °C"}}), AMD_TOPOLOGY)
    assert d.temp_sensor == f"{AMD_NAME}/Temperatures/Core Max"
    d = detect(make_tree(AMD_NAME, {"Temperatures": {"Board": "40 °C", "VRM": "62 °C"}}), AMD_TOPOLOGY)
    assert d.temp_sensor == f"{AMD_NAME}/Temperatures/VRM"
    assert d.alternatives["temp_sensor"] == [f"{AMD_NAME}/Temperatures/Board"]


def test_tree_without_cpu_hardware_has_no_sensor_fields_but_cpus() -> None:
    d = detect(make_tree("Motherboard", {"Temperatures": {"System": "40 °C"}}), AMD_TOPOLOGY)
    assert d.power_sensor is None
    assert d.temp_sensor is None
    assert d.clock_sensors == []
    assert d.cpus == [0, 4, 8, 12]
    assert to_config_fields(d) == {"cpus": [0, 4, 8, 12], "tdp_w": 120.0, "tjmax_c": 89.0, "pkg_max_w": 162.0}


def test_unknown_cpu_gives_no_tdp_and_fallback_columns_only() -> None:
    name = "Mystery Chip 9000"
    sensors = {
        "Powers": {"Package": "42 W", "Cores": "30 W"},
        "Temperatures": {"Core (Tctl/Tdie)": "55 °C", "Hot": "80 °C"},
    }
    d = detect(make_tree(name, sensors), CpuTopology(name, 8, 16))
    assert d.tdp_w is None and d.tjmax_c is None and d.pkg_max_w is None
    assert d.power_sensor == f"{name}/Powers/Package"
    assert d.temp_sensor == f"{name}/Temperatures/Hot"
    assert "tdp_w" not in to_config_fields(d)


def test_find_cpu_hardware_prefers_exact_name() -> None:
    tree = make_machine([
        hardware_node("GPU", {"Powers": {"Board": "10 W"}, "Clocks": {"Core": "1 MHz"}}),
        hardware_node(AMD_NAME, {"Powers": {"Package": "1 W"}, "Clocks": {"Core #1": "1 MHz"}}),
    ])
    assert find_cpu_hardware(tree, AMD_NAME) == AMD_NAME


def test_find_cpu_hardware_first_qualifying_node_wins() -> None:
    tree = make_machine([
        hardware_node("First CPU", {"Powers": {"Package": "1 W"}, "Clocks": {"Core #1": "1 MHz"}}),
        hardware_node("Second CPU", {"Powers": {"Package": "2 W"}, "Clocks": {"Core #1": "2 MHz"}}),
    ])
    assert find_cpu_hardware(tree, "Unknown") == "First CPU"


def test_two_qualifying_nodes_first_wins_in_detect() -> None:
    tree = make_machine([
        hardware_node("First CPU", {"Powers": {"Package": "1 W"}, "Clocks": {"Core #1": "1 MHz"}}),
        hardware_node("Second CPU", {"Powers": {"Package": "2 W"}, "Clocks": {"Core #1": "2 MHz"}}),
    ])
    d = detect(tree, CpuTopology("Unknown Chip", 4, 4))
    assert d.power_sensor == "First CPU/Powers/Package"


def test_logical_not_divisible_by_physical() -> None:
    assert core_cpus(6, 13) == [0, 2, 6, 8]


def test_fixture_trees_when_present() -> None:
    amd = FIXTURES / "lhm_amd.json"
    if not amd.exists():
        pytest.skip("tests/fixtures/lhm_amd.json not present")
    import json

    tree = json.loads(amd.read_text(encoding="utf-8"))
    d = detect(tree, AMD_TOPOLOGY)
    assert d.power_sensor is not None
    assert d.cpus == [0, 4, 8, 12]


def test_tdp_table_lookup() -> None:
    assert lookup(AMD_NAME) == (120.0, 89.0)
    assert lookup("Unknown") is None


def test_tdp_lookup_strips_registry_core_suffix() -> None:
    assert lookup("AMD Ryzen 7 7800X3D 8-Core Processor") == (120.0, 89.0)
    assert lookup("  AMD Ryzen 7 7800X3D 8-Core Processor  ") == (120.0, 89.0)
    assert lookup("AMD Ryzen 7 7800X3D 8-Core Processor X") is None


def test_detect_with_registry_name_gets_tdp() -> None:
    name = "AMD Ryzen 7 7800X3D 8-Core Processor"
    d = detect(make_tree(AMD_NAME, AMD_SENSORS), CpuTopology(name, 8, 16))
    assert d.tdp_w == 120.0
    assert d.tjmax_c == 89.0
    assert d.pkg_max_w == 162.0
