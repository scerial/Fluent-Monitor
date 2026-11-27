from __future__ import annotations

MAX_PARTIAL = 1 << 20


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        return raw.decode("cp949", errors="replace")
    return text


def _strip_cr(raw: bytes) -> bytes:
    return raw[:-1] if raw.endswith(b"\r") else raw


class LineAssembler:
    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[str]:
        self._buf.extend(chunk)
        lines: list[str] = []
        start = 0
        while True:
            end = self._buf.find(b"\n", start)
            if end < 0:
                break
            lines.append(_decode(_strip_cr(bytes(self._buf[start:end]))))
            start = end + 1
        del self._buf[:start]
        if len(self._buf) > MAX_PARTIAL:
            lines.append(_decode(_strip_cr(bytes(self._buf))))
            self._buf.clear()
        return lines

    def reset(self) -> None:
        self._buf.clear()
