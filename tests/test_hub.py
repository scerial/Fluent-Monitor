from __future__ import annotations

import asyncio
import math
import threading
import time

import orjson

from fluentmon.api.bus import Bus
from fluentmon.api.hub import Client, Hub, PcPoller, client_writer


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed_with: list[int] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with.append(code)


async def settle(seconds: float = 0.02) -> None:
    await asyncio.sleep(seconds)


def sys_msg() -> dict:
    return {"t": "sys", "cpu_ids": [0], "cpu": [50.0]}


def test_broadcast_requests_one_poll_and_sys_follows() -> None:
    async def main() -> None:
        samples: list[float] = []

        def sample() -> dict:
            samples.append(time.monotonic())
            return sys_msg()

        hub_ref: dict[str, Hub] = {}
        poller = PcPoller(lambda msg: hub_ref["hub"].send(msg), sample, min_gap_s=0.0)
        hub = Hub(poller, idle_s=60.0)
        hub_ref["hub"] = hub
        requests: list[int] = []
        original = poller.request

        def counted() -> None:
            requests.append(1)
            original()

        setattr(poller, "request", counted)
        ws = FakeWs()
        client = Client(ws)
        hub.clients.add(client)
        writer = asyncio.create_task(client_writer(client))
        hub.broadcast({"t": "resid", "rows": []})
        await settle()
        assert len(requests) == 1
        assert [orjson.loads(data)["t"] for data in ws.sent] == ["resid", "sys"]
        assert len(samples) == 1
        writer.cancel()
        if hub.idle:
            hub.idle.cancel()


def test_six_requests_poll_at_most_twice_and_spaced() -> None:
    async def main() -> None:
        starts: list[float] = []
        running = threading.Lock()
        overlap: list[bool] = []

        def sample() -> dict:
            if not running.acquire(blocking=False):
                overlap.append(True)
                return sys_msg()
            try:
                starts.append(time.monotonic())
                time.sleep(0.01)
            finally:
                running.release()
            return sys_msg()

        sent: list[dict] = []
        poller = PcPoller(sent.append, sample)
        for _ in range(6):
            poller.request()
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.4)
        assert len(starts) <= 2
        assert not overlap
        if len(starts) == 2:
            assert starts[1] - starts[0] >= 0.19
        assert len(sent) == len(starts)

    asyncio.run(main())


def test_idle_poll_runs_after_silence_and_again() -> None:
    async def main() -> None:
        polls: list[float] = []
        idle_calls: list[int] = []

        def sample() -> dict:
            polls.append(time.monotonic())
            return sys_msg()

        poller = PcPoller(lambda msg: None, sample, min_gap_s=0.01)
        hub = Hub(poller, idle_s=0.05)
        hub.on_idle = lambda: idle_calls.append(1)
        hub.start()
        await asyncio.sleep(0.2)
        if hub.idle:
            hub.idle.cancel()
        assert len(polls) >= 2
        assert len(idle_calls) >= 2

    asyncio.run(main())


def test_console_filter_skips_clients_with_console_off() -> None:
    async def main() -> None:
        poller = PcPoller(lambda msg: None, sys_msg, min_gap_s=0.0)
        hub = Hub(poller, idle_s=60.0)
        hub.start()
        on_ws, off_ws = FakeWs(), FakeWs()
        on_client = Client(on_ws)
        off_client = Client(off_ws, console=False)
        hub.clients.update({on_client, off_client})
        hub.send({"t": "con", "first": 1, "lines": ["x"], "kinds": "n"})
        hub.send({"t": "resid", "rows": []})
        assert on_client.queue.qsize() == 2
        assert off_client.queue.qsize() == 1
        if hub.idle:
            hub.idle.cancel()

    asyncio.run(main())


def test_full_queue_closes_only_that_client() -> None:
    async def main() -> None:
        poller = PcPoller(lambda msg: None, sys_msg, min_gap_s=0.0)
        hub = Hub(poller, idle_s=60.0)
        hub.start()
        slow_ws, fast_ws = FakeWs(), FakeWs()
        slow = Client(slow_ws, queue=asyncio.Queue(maxsize=1))
        fast = Client(fast_ws)
        hub.clients.update({slow, fast})
        hub.send({"t": "resid", "rows": []})
        hub.send({"t": "resid", "rows": []})
        await settle(0.01)
        assert slow_ws.closed_with == [4408]
        assert fast.queue.qsize() == 2
        hub.send({"t": "resid", "rows": []})
        await settle(0.01)
        assert slow_ws.closed_with == [4408]
        if hub.idle:
            hub.idle.cancel()

    asyncio.run(main())


def test_send_to_targets_one_client() -> None:
    async def main() -> None:
        poller = PcPoller(lambda msg: None, sys_msg, min_gap_s=0.0)
        hub = Hub(poller, idle_s=60.0)
        hub.start()
        first = Client(FakeWs())
        second = Client(FakeWs())
        hub.clients.update({first, second})
        hub.send_to(first, {"t": "hello"})
        assert first.queue.qsize() == 1
        assert second.queue.qsize() == 0
        if hub.idle:
            hub.idle.cancel()

    asyncio.run(main())


def test_nan_is_encoded_as_null() -> None:
    async def main() -> None:
        poller = PcPoller(lambda msg: None, sys_msg, min_gap_s=0.0)
        hub = Hub(poller, idle_s=60.0)
        hub.start()
        client = Client(FakeWs())
        hub.clients.add(client)
        hub.send({"t": "x", "v": math.nan})
        data = client.queue.get_nowait()
        assert orjson.loads(data) == {"t": "x", "v": None}
        if hub.idle:
            hub.idle.cancel()

    asyncio.run(main())


def test_writer_delivers_in_order() -> None:
    async def main() -> None:
        ws = FakeWs()
        client = Client(ws)
        writer = asyncio.create_task(client_writer(client))
        for n in range(5):
            client.queue.put_nowait(orjson.dumps({"n": n}))
        await settle()
        writer.cancel()
        assert [orjson.loads(data)["n"] for data in ws.sent] == [0, 1, 2, 3, 4]

    asyncio.run(main())


def test_poller_survives_sample_exception() -> None:
    async def main() -> None:
        calls: list[int] = []
        sent: list[dict] = []

        def sample() -> dict:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("lhm down")
            return sys_msg()

        poller = PcPoller(sent.append, sample, min_gap_s=0.0)
        poller.request()
        await settle()
        assert sent == []
        poller.request()
        await settle()
        assert len(sent) == 1

    asyncio.run(main())


def test_bus_publish_subscribe_unsubscribe() -> None:
    async def main() -> None:
        bus = Bus()
        first = bus.subscribe()
        second = bus.subscribe()
        bus.publish("a")
        assert first.get_nowait() == "a"
        assert second.get_nowait() == "a"
        bus.unsubscribe(second)
        bus.publish("b")
        assert first.get_nowait() == "b"
        assert second.empty()

    asyncio.run(main())
