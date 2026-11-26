from __future__ import annotations

import logging
import socket

import psutil
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger("fluentmon.discovery")

SERVICE_TYPE = "_fluentmon._tcp.local."


def lan_addresses() -> list[bytes]:
    addresses: list[bytes] = []
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family == socket.AF_INET and not entry.address.startswith("127."):
                addresses.append(socket.inet_aton(entry.address))
    return addresses


class Advertiser:
    def __init__(self, port: int, version: str) -> None:
        self.port = port
        self.version = version
        self._zeroconf: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    async def start(self) -> None:
        try:
            host = socket.gethostname()
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{host}.{SERVICE_TYPE}",
                port=self.port,
                properties={"proto": "1", "ver": self.version, "name": host},
                server=f"{host}.local.",
                addresses=lan_addresses() or None,
            )
            self._zeroconf = AsyncZeroconf()
            await self._zeroconf.async_register_service(self._info)
        except Exception:
            log.exception("Bonjour advertisement failed; the server still runs")

    async def stop(self) -> None:
        try:
            if self._zeroconf is not None and self._info is not None:
                await self._zeroconf.async_unregister_service(self._info)
            if self._zeroconf is not None:
                await self._zeroconf.async_close()
        except Exception:
            log.exception("Bonjour withdrawal failed")
        finally:
            self._zeroconf = None
            self._info = None
