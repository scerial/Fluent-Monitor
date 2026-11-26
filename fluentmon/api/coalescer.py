from __future__ import annotations

import asyncio
from collections.abc import Sequence

from fluentmon.api.hub import Hub


class Coalescer:
    def __init__(self, hub: Hub, window_s: float = 0.1, max_lines: int = 400) -> None:
        self.hub = hub
        self.window_s = window_s
        self.max_lines = max_lines
        self._window: asyncio.TimerHandle | None = None
        self._rows: list[tuple[int, list[float]]] = []
        self._bars: dict | None = None
        self._lines: list[tuple[int, str, str]] = []

    def push_rows(self, rows: Sequence[tuple[int, list[float]]], bars: dict) -> None:
        self._rows.extend(rows)
        self._bars = bars
        self._submit()

    def push_lines(self, lines: Sequence[tuple[int, str, str]]) -> None:
        self._lines.extend(lines)
        self._submit()

    def push_event(self, msg: dict) -> None:
        self._cancel_window()
        self._flush_pending()
        self.hub.broadcast(msg)
        self._open_window()

    def flush(self) -> None:
        self._cancel_window()
        if self._flush_pending():
            self._open_window()

    def drop_pending(self) -> None:
        self._rows = []
        self._bars = None
        self._lines = []

    def close(self) -> None:
        self._cancel_window()
        self.drop_pending()

    def _submit(self) -> None:
        if self._window is None and self._flush_pending():
            self._open_window()

    def _open_window(self) -> None:
        self._window = asyncio.get_running_loop().call_later(self.window_s, self._window_end)

    def _cancel_window(self) -> None:
        if self._window is not None:
            self._window.cancel()
            self._window = None

    def _window_end(self) -> None:
        self._window = None
        if self._flush_pending():
            self._open_window()

    def _flush_pending(self) -> bool:
        sent = False
        if self._rows:
            self.hub.broadcast({"t": "resid", "rows": [[it, values] for it, values in self._rows]})
            self._rows = []
            sent = True
        if self._bars is not None:
            self.hub.broadcast(self._bars)
            self._bars = None
            sent = True
        for start in range(0, len(self._lines), self.max_lines):
            chunk = self._lines[start : start + self.max_lines]
            self.hub.broadcast(
                {
                    "t": "con",
                    "first": chunk[0][0],
                    "lines": [text for _, _, text in chunk],
                    "kinds": "".join(kind for _, kind, _ in chunk),
                }
            )
            sent = True
        self._lines = []
        return sent
