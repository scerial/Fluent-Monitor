from __future__ import annotations

import asyncio
import io
import sys
from typing import Any

import pytest
import qrcode

import fluentmon.discovery as discovery
import fluentmon.pairing as pairing
from fluentmon.config import Config
from fluentmon.discovery import SERVICE_TYPE, Advertiser
from fluentmon.pairing import pairing_url, print_pairing


def test_pairing_url_is_the_section_nine_form() -> None:
    assert pairing_url("192.168.1.20", 8765, "abc") == "fluentmon://connect?host=192.168.1.20&port=8765&token=abc"


def test_print_pairing_prints_url_and_exact_qr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "lan_ipv4", lambda: "192.168.1.20")
    cfg = Config()
    cfg.server.token = "abc"
    out = io.StringIO()
    print_pairing(cfg, out)
    url = "fluentmon://connect?host=192.168.1.20&port=8765&token=abc"
    expected_qr = qrcode.QRCode()
    expected_qr.add_data(url)
    expected_qr.make(fit=True)
    qr_text = io.StringIO()
    expected_qr.print_ascii(out=qr_text)
    assert out.getvalue() == url + "\n" + qr_text.getvalue()


def test_no_network_uses_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "lan_ipv4", lambda: None)
    cfg = Config()
    cfg.server.token = "abc"
    out = io.StringIO()
    print_pairing(cfg, out)
    assert out.getvalue().splitlines()[0] == "fluentmon://connect?host=localhost&port=8765&token=abc"


def test_print_pairing_without_any_stream_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "lan_ipv4", lambda: "192.168.1.20")
    monkeypatch.setattr(sys, "stdout", None)
    cfg = Config()
    cfg.server.token = "abc"
    print_pairing(cfg)


class FakeZeroconf:
    registered: list[Any] = []
    closed: list[bool] = []

    def __init__(self) -> None:
        pass

    async def async_register_service(self, info: Any) -> None:
        FakeZeroconf.registered.append(info)

    async def async_unregister_service(self, info: Any) -> None:
        return None

    async def async_close(self) -> None:
        FakeZeroconf.closed.append(True)


def test_advertiser_registers_service_type_once_with_txt_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeZeroconf.registered = []
    FakeZeroconf.closed = []
    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeZeroconf)

    async def main() -> None:
        advertiser = Advertiser(8765, "0.1.0")
        await advertiser.start()
        await advertiser.stop()

    asyncio.run(main())
    assert len(FakeZeroconf.registered) == 1
    info = FakeZeroconf.registered[0]
    assert info.type == SERVICE_TYPE
    assert info.port == 8765
    assert set(info.properties) == {b"proto", b"ver", b"name"}
    assert info.properties[b"proto"] == b"1"
    assert info.properties[b"ver"] == b"0.1.0"


def test_advertiser_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def __init__(self) -> None:
            raise OSError("no multicast")

    monkeypatch.setattr(discovery, "AsyncZeroconf", Broken)

    async def main() -> None:
        await Advertiser(8765, "0.1.0").start()

    asyncio.run(main())
