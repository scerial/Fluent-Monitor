from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import httpx

from fluentmon.config import ConfigError

log = logging.getLogger("fluentmon.notify")

NOTIFY_KINDS = frozenset({"converged", "problem", "idle", "session_end"})
TOPIC_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
NTFY_URL = "https://ntfy.sh/{topic}"


class Notifier:
    def __init__(
        self,
        topic: str,
        client: httpx.AsyncClient | None = None,
        dedupe_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if topic and not TOPIC_PATTERN.fullmatch(topic):
            raise ConfigError("ntfy_topic may contain only letters, digits, '_' and '-'")
        self.topic = topic
        self._client = client
        self.dedupe_s = dedupe_s
        self._clock = clock
        self._last_problem: float | None = None

    async def handle(self, ev: dict) -> None:
        if not self.topic:
            return
        kind = ev.get("kind")
        if kind not in NOTIFY_KINDS:
            return
        if kind == "problem":
            now = self._clock()
            if self._last_problem is not None and now - self._last_problem < self.dedupe_s:
                return
            self._last_problem = now
        text = ev.get("text") or ""
        body = f"{kind}: {text}" if text else str(kind)
        client = self._client if self._client is not None else httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.post(
                NTFY_URL.format(topic=self.topic),
                content=body,
                headers={"Title": "FluentMon", "Priority": "default"},
            )
            if response.status_code >= 400:
                log.warning("ntfy answered %s", response.status_code)
        except httpx.HTTPError:
            log.warning("ntfy notification failed", exc_info=True)
        finally:
            if self._client is None:
                await client.aclose()
