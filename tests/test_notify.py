from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from fluentmon.config import ConfigError
from fluentmon.notify import Notifier


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def recording_client(status: int = 200, error: Callable[[httpx.Request], Exception] | None = None) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if error is not None:
            raise error(request)
        return httpx.Response(status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


def run(coro: Coroutine[Any, Any, None]) -> None:
    asyncio.run(coro)


def test_converged_posts_once_and_iter_con_do_not() -> None:
    async def main() -> None:
        client, requests = recording_client()
        notifier = Notifier("my-topic_1", client=client)
        await notifier.handle({"t": "event", "kind": "converged", "iter": 1412, "text": "! 1412 solution is converged", "ts": "x"})
        await notifier.handle({"t": "event", "kind": "run_start", "iter": 1, "text": "run started", "ts": "x"})
        await notifier.handle({"t": "iter"})
        await notifier.handle({"t": "con", "lines": ["x"]})
        assert len(requests) == 1
        assert str(requests[0].url) == "https://ntfy.sh/my-topic_1"
        assert requests[0].headers["Title"] == "FluentMon"
        assert requests[0].content == b"converged: ! 1412 solution is converged"

    run(main())


def test_problem_is_deduplicated_within_five_minutes() -> None:
    async def main() -> None:
        client, requests = recording_client()
        clock = Clock()
        notifier = Notifier("topic", client=client, clock=clock)
        event = {"t": "event", "kind": "problem", "iter": 5, "text": "Divergence detected", "ts": "x"}
        await notifier.handle(event)
        clock.now += 10.0
        await notifier.handle(event)
        assert len(requests) == 1

    run(main())


def test_problem_after_window_posts_again() -> None:
    async def main() -> None:
        client, requests = recording_client()
        clock = Clock()
        notifier = Notifier("topic", client=client, clock=clock)
        event = {"t": "event", "kind": "problem", "iter": 5, "text": "Divergence detected", "ts": "x"}
        await notifier.handle(event)
        clock.now += 301.0
        await notifier.handle(event)
        assert len(requests) == 2

    run(main())


def test_idle_and_session_end_are_sent() -> None:
    async def main() -> None:
        client, requests = recording_client()
        notifier = Notifier("topic", client=client)
        await notifier.handle({"t": "event", "kind": "idle", "iter": 3, "text": "", "ts": "x"})
        await notifier.handle({"t": "event", "kind": "session_end", "iter": 3, "text": "session ended", "ts": "x"})
        assert len(requests) == 2
        assert requests[0].content == b"idle"

    run(main())


def test_empty_topic_sends_nothing() -> None:
    async def main() -> None:
        client, requests = recording_client()
        notifier = Notifier("", client=client)
        await notifier.handle({"t": "event", "kind": "converged", "iter": 1, "text": "x", "ts": "x"})
        assert requests == []

    run(main())


def test_server_error_does_not_raise() -> None:
    async def main() -> None:
        client, requests = recording_client(status=500)
        notifier = Notifier("topic", client=client)
        await notifier.handle({"t": "event", "kind": "converged", "iter": 1, "text": "x", "ts": "x"})
        assert len(requests) == 1

    run(main())


def test_network_error_is_swallowed() -> None:
    async def main() -> None:
        client, requests = recording_client(error=lambda request: httpx.ConnectError("down", request=request))
        notifier = Notifier("topic", client=client)
        await notifier.handle({"t": "event", "kind": "converged", "iter": 1, "text": "x", "ts": "x"})

    run(main())


@pytest.mark.parametrize("topic", ["bad topic", "a/b", "x?y=1", "émoji"])
def test_topic_with_unsafe_characters_is_rejected(topic: str) -> None:
    with pytest.raises(ConfigError):
        Notifier(topic)
