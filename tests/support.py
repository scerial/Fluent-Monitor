from __future__ import annotations


def hardware_node(hardware: str, sensors: dict[str, dict[str, str]]) -> dict:
    groups = [
        {
            "Text": group,
            "Children": [{"Text": name, "Value": value, "Children": []} for name, value in items.items()],
        }
        for group, items in sensors.items()
    ]
    return {"Text": hardware, "Children": groups}


def make_machine(nodes: list[dict]) -> dict:
    return {"Text": "Sensor", "Children": [{"Text": "TESTPC", "Children": nodes}]}


def make_tree(hardware: str, sensors: dict[str, dict[str, str]]) -> dict:
    return make_machine([hardware_node(hardware, sensors)])
