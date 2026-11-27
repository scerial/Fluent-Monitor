from __future__ import annotations

import socket
import sys
from typing import IO

import psutil
import qrcode

from fluentmon.config import Config


def pairing_url(host: str, port: int, token: str) -> str:
    return f"fluentmon://connect?host={host}&port={port}&token={token}"


def lan_ipv4() -> str | None:
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family == socket.AF_INET and not entry.address.startswith("127."):
                return entry.address
    return None


def print_pairing(cfg: Config, out: IO[str] | None = None) -> None:
    stream = out if out is not None else sys.stdout
    if stream is None or (out is None and not stream.isatty()):
        return
    host = lan_ipv4() or "localhost"
    url = pairing_url(host, cfg.server.port, cfg.server.token)
    print(url, file=stream)
    code = qrcode.QRCode()
    code.add_data(url)
    code.make(fit=True)
    code.print_ascii(out=stream)
