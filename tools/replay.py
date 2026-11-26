from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from fluentmon.config import load_config
from fluentmon.transcript.parser import DATA


def is_data_line(raw: bytes) -> bool:
    return DATA.match(raw.decode("ascii", errors="replace").rstrip("\r\n")) is not None


def ticks(lines: Sequence[bytes]) -> list[list[bytes]]:
    groups: list[list[bytes]] = []
    current: list[bytes] = []
    for raw in lines:
        if current and is_data_line(raw):
            groups.append(current)
            current = []
        current.append(raw)
    if current:
        groups.append(current)
    return groups


def default_output() -> Path:
    return Path(load_config().server.watch_dir) / "replay.trn"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="replay")
    parser.add_argument("fixture")
    parser.add_argument("--ips", type=float, default=10.0)
    parser.add_argument("--burst", action="store_true")
    parser.add_argument("--out", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fixture = Path(args.fixture)
    if not fixture.is_file():
        print(f"fixture not found: {fixture}", file=sys.stderr)
        return 2
    if args.ips <= 0:
        print("--ips must be above 0", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else default_output()
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = fixture.read_bytes().splitlines(keepends=True)
    groups = [lines] if args.burst else ticks(lines)
    delay = 1.0 / args.ips
    try:
        with out.open("wb") as handle:
            for index, group in enumerate(groups):
                if index:
                    time.sleep(delay)
                handle.write(b"".join(group))
                handle.flush()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
