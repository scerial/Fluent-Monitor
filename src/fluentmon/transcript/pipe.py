from __future__ import annotations

import logging
from pathlib import Path
from typing import IO, BinaryIO

log = logging.getLogger("fluentmon.transcript.pipe")

CHUNK = 64 * 1024


class PipeSource:
    def __init__(self, stream: IO[bytes], out: Path) -> None:
        self._stream = stream
        self._out = out

    def run(self) -> None:
        writer: BinaryIO | None
        try:
            writer = self._out.open("ab")
        except OSError:
            log.exception("cannot open %s for the pipe copy", self._out)
            writer = None
        reader = getattr(self._stream, "read1", self._stream.read)
        try:
            while True:
                chunk = reader(CHUNK)
                if not chunk:
                    break
                if writer is None:
                    continue
                try:
                    writer.write(chunk)
                    writer.flush()
                except OSError:
                    log.exception("pipe copy to %s failed; still draining the pipe", self._out)
                    writer.close()
                    writer = None
        finally:
            if writer is not None:
                writer.close()
