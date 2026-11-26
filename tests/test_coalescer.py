from __future__ import annotations

import asyncio

import orjson

from fluentmon.api.coalescer import Coalescer
from fluentmon.api.hub import Client, Hub, PcPoller, client_writer


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(orjson.loads(data))

    async def close(self, code: int = 1000) -> None:
        return None


class Setup:
    def __init__(self, window_s: float = 0.05, max_lines: int = 400) -> None:
        self.ws = FakeWs()
        poller = PcPoller(lambda msg: None, lambda: {"t": "sys"}, min_gap_s=0.0)
        self.hub = Hub(poller, idle_s=60.0)
        self.client = Client(self.ws)
        self.hub.clients.add(self.client)
        self.writer = asyncio.create_task(client_writer(self.client))
        self.coalescer = Coalescer(self.hub, window_s=window_s, max_lines=max_lines)
        self.hub.start()

    async def close(self) -> None:
        self.coalescer.close()
        if self.hub.idle:
            self.hub.idle.cancel()
        self.writer.cancel()

    def types(self) -> list[str]:
        return [msg["t"] for msg in self.ws.sent if msg["t"] != "sys"]


async def settle(seconds: float = 0.01) -> None:
    await asyncio.sleep(seconds)


def bars(n: int) -> dict:
    return {"t": "bars", "iter": {"done": n}, "conv": None, "step": None}


def test_first_push_is_sent_at_once() -> None:
    async def main() -> None:
        setup = Setup()
        setup.coalescer.push_rows([(1, [1.0])], bars(1))
        await settle()
        assert setup.types() == ["resid", "bars"]
        await setup.close()

    asyncio.run(main())


def test_pushes_inside_window_merge() -> None:
    async def main() -> None:
        setup = Setup(window_s=0.05)
        setup.coalescer.push_rows([(1, [1.0])], bars(1))
        for n in range(2, 12):
            setup.coalescer.push_rows([(n, [float(n)])], bars(n))
        await settle(0.1)
        resids = [msg for msg in setup.ws.sent if msg["t"] == "resid"]
        bar_msgs = [msg for msg in setup.ws.sent if msg["t"] == "bars"]
        assert len(resids) == 2
        assert [row[0] for row in resids[1]["rows"]] == list(range(2, 12))
        assert len(bar_msgs) == 2
        assert bar_msgs[-1]["iter"]["done"] == 11
        await setup.close()

    asyncio.run(main())


def test_thousand_lines_become_three_con_messages() -> None:
    async def main() -> None:
        setup = Setup()
        lines = [(n, "n", f"line {n}") for n in range(1, 1001)]
        setup.coalescer.push_lines(lines)
        await settle()
        cons = [msg for msg in setup.ws.sent if msg["t"] == "con"]
        assert len(cons) == 3
        assert all(len(msg["lines"]) <= 400 for msg in cons)
        assert [msg["first"] for msg in cons] == [1, 401, 801]
        assert sum(len(msg["lines"]) for msg in cons) == 1000
        assert cons[0]["kinds"] == "n" * 400
        await setup.close()

    asyncio.run(main())


def test_event_keeps_order_after_rows() -> None:
    async def main() -> None:
        setup = Setup(window_s=0.05)
        setup.coalescer.push_rows([(1, [1.0])], bars(1))
        setup.coalescer.push_rows([(2, [2.0])], bars(2))
        setup.coalescer.push_event({"t": "event", "kind": "converged"})
        await settle()
        assert setup.types() == ["resid", "bars", "resid", "bars", "event"]
        await setup.close()

    asyncio.run(main())


def test_drop_pending_discards_waiting_data() -> None:
    async def main() -> None:
        setup = Setup(window_s=0.05)
        setup.coalescer.push_rows([(1, [1.0])], bars(1))
        setup.coalescer.push_rows([(2, [2.0])], bars(2))
        setup.coalescer.drop_pending()
        await settle(0.1)
        assert setup.types() == ["resid", "bars"]
        await setup.close()

    asyncio.run(main())


def test_close_cancels_window() -> None:
    async def main() -> None:
        setup = Setup(window_s=0.05)
        setup.coalescer.push_rows([(1, [1.0])], bars(1))
        setup.coalescer.push_rows([(2, [2.0])], bars(2))
        setup.coalescer.close()
        assert setup.coalescer._window is None
        await settle(0.1)
        assert setup.types() == ["resid", "bars"]
        await setup.close()

    asyncio.run(main())


def test_no_window_opens_when_nothing_was_sent() -> None:
    async def main() -> None:
        setup = Setup(window_s=0.05)
        setup.coalescer.push_rows([(1, [1.0])], bars(1))
        await settle(0.1)
        assert setup.coalescer._window is None
        setup.coalescer.push_lines([(2, "n", "now")])
        await settle()
        assert setup.types() == ["resid", "bars", "con"]
        await setup.close()

    asyncio.run(main())


def test_lines_inside_window_are_split_without_losing_numbers() -> None:
    async def main() -> None:
        setup = Setup(window_s=0.05, max_lines=4)
        setup.coalescer.push_lines([(1, "n", "a")])
        setup.coalescer.push_lines([(n, "n", f"b{n}") for n in range(2, 11)])
        await settle(0.12)
        cons = [msg for msg in setup.ws.sent if msg["t"] == "con"]
        numbers: list[int] = []
        for msg in cons:
            assert len(msg["lines"]) <= 4
            numbers.extend(range(msg["first"], msg["first"] + len(msg["lines"])))
        assert numbers == list(range(1, 11))
        await setup.close()

    asyncio.run(main())
