from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fluentmon.system.power import LhmClient, find_sensor, parse_value

FIXTURE = Path(__file__).parent / "fixtures" / "lhm_amd.json"

HARDWARE = "AMD Ryzen 7 7800X3D"
POWER = f"{HARDWARE}/Powers/Package"
TEMP = f"{HARDWARE}/Temperatures/Core (Tctl/Tdie)"
CLOCKS = ["Core #1 (Effective)", "Core #3 (Effective)", "Core #5 (Effective)", "Core #7 (Effective)"]


def leaf(text: str, value: str = "") -> dict:
    return {"id": 0, "Text": text, "Min": "", "Value": value, "Max": "", "Children": []}


def amd_tree() -> dict:
    return {
        "id": 0,
        "Text": "Sensor",
        "Min": "",
        "Value": "",
        "Max": "",
        "Children": [
            {
                "id": 1,
                "Text": "DESKTOP",
                "Min": "",
                "Value": "",
                "Max": "",
                "Children": [
                    {
                        "id": 2,
                        "Text": HARDWARE,
                        "Min": "",
                        "Value": "",
                        "Max": "",
                        "Children": [
                            {
                                "id": 3,
                                "Text": "Clocks",
                                "Min": "",
                                "Value": "",
                                "Max": "",
                                "Children": [
                                    leaf("Core #1 (Effective)", "4950.0 MHz"),
                                    leaf("Core #3 (Effective)", "4925 MHz"),
                                    leaf("Core #5 (Effective)", "-"),
                                ],
                            },
                            {
                                "id": 4,
                                "Text": "Temperatures",
                                "Min": "",
                                "Value": "",
                                "Max": "",
                                "Children": [leaf("Core (Tctl/Tdie)", "68.0 °C")],
                            },
                            {
                                "id": 5,
                                "Text": "Powers",
                                "Min": "",
                                "Value": "",
                                "Max": "",
                                "Children": [leaf("Package", "71.4 W")],
                            },
                        ],
                    }
                ],
            }
        ],
    }


def test_parse_value_units_and_separators() -> None:
    assert parse_value("87.3 W") == 87.3
    assert parse_value("87,3 W") == 87.3
    assert parse_value("4950.0 MHz") == 4950.0
    assert parse_value("4.95 GHz") == pytest.approx(4950.0)
    assert parse_value("68.0 °C") == 68.0


def test_parse_value_rejects_placeholders() -> None:
    assert parse_value("-") is None
    assert parse_value("") is None
    assert parse_value("n/a") is None


def test_find_sensor_follows_exact_path() -> None:
    tree = amd_tree()
    assert find_sensor(tree, POWER) == 71.4
    assert find_sensor(tree, TEMP) == 68.0
    assert find_sensor(tree, f"{HARDWARE}/Powers/Missing") is None
    assert find_sensor(tree, "Nope/Powers/Package") is None


def test_find_sensor_on_fixture_when_present() -> None:
    if not FIXTURE.exists():
        pytest.skip("lhm_amd.json fixture not recorded yet")
    tree = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert find_sensor(tree, POWER) is not None


def client_for(handler: httpx.MockTransport) -> LhmClient:
    return LhmClient(
        "http://127.0.0.1:8085",
        POWER,
        CLOCKS,
        TEMP,
        transport=handler,
    )


def test_read_good_tree() -> None:
    body = json.dumps(amd_tree())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    client = client_for(transport)
    reading = client.read()
    assert client.ok is True
    assert reading.pkg_w == 71.4
    assert reading.tctl_c == 68.0
    assert reading.clk_mhz == [4950.0, 4925.0, None, None]


def test_clock_is_none_without_hardware_reference() -> None:
    body = json.dumps(amd_tree())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    client = LhmClient("http://127.0.0.1:8085", None, CLOCKS, None, transport=transport)
    reading = client.read()
    assert reading.clk_mhz == [None] * 4
    assert reading.pkg_w is None
    assert reading.tctl_c is None


def test_server_error_gives_all_none() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    client = client_for(transport)
    reading = client.read()
    assert client.ok is False
    assert reading.pkg_w is None
    assert reading.tctl_c is None
    assert reading.clk_mhz == [None] * 4


def test_timeout_gives_all_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = client_for(httpx.MockTransport(handler))
    reading = client.read()
    assert client.ok is False
    assert reading.pkg_w is None


def test_malformed_json_gives_all_none() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    client = client_for(transport)
    reading = client.read()
    assert client.ok is False
    assert reading.pkg_w is None


def test_missing_sensor_is_none_not_error() -> None:
    body = json.dumps(leaf("Sensor"))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    client = client_for(transport)
    reading = client.read()
    assert client.ok is True
    assert reading.pkg_w is None
