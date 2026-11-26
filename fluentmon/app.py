from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from fluentmon import __version__
from fluentmon.api.app import create_app
from fluentmon.api.bus import Bus
from fluentmon.api.coalescer import Coalescer
from fluentmon.api.hub import Hub, PcPoller
from fluentmon.config import Config, ConfigError
from fluentmon.discovery import Advertiser
from fluentmon.notify import Notifier
from fluentmon.pairing import print_pairing
from fluentmon.record import RecorderClient
from fluentmon.system.cpu import prime
from fluentmon.system.memory import FluentTree
from fluentmon.system.power import LhmClient
from fluentmon.system.sampler import Sampler
from fluentmon.transcript.manager import SessionChanges, SessionEnded, SessionManager, SessionStarted

log = logging.getLogger("fluentmon.app")

MANAGER_RESTART_S = 1.0


def build_app(cfg: Config, record: Path | None = None) -> FastAPI:
    bus = Bus()
    lhm = LhmClient(cfg.meters.lhm_url, cfg.meters.power_sensor, cfg.meters.clock_sensors, cfg.meters.temp_sensor)
    tree = FluentTree(None, cfg.meters.fluent_ram_s)
    sampler = Sampler(cfg, lhm, tree)
    poller = PcPoller(lambda msg: hub.send(msg), sampler, cfg.meters.poll_min_ms / 1000)
    hub = Hub(poller, cfg.meters.idle_poll_s)
    coalescer = Coalescer(hub)
    manager = SessionManager(cfg, bus)
    hub.on_idle = manager.check_idle
    notifier = Notifier(cfg.server.ntfy_topic)
    notify_tasks: set[asyncio.Task[None]] = set()
    app = create_app(cfg, hub, manager, sampler)

    async def supervise_manager() -> None:
        while True:
            try:
                await manager.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("session manager failed; restarting in %s s", MANAGER_RESTART_S)
                await asyncio.sleep(MANAGER_RESTART_S)

    async def pump(queue: asyncio.Queue[object]) -> None:
        while True:
            ev = await queue.get()
            try:
                _handle(ev)
            except Exception:
                log.exception("pump failed on %s", type(ev).__name__)

    def _handle(ev: object) -> None:
        if isinstance(ev, SessionStarted):
            coalescer.drop_pending()
            hub.broadcast(ev.snapshot)
        elif isinstance(ev, SessionChanges):
            if ev.snapshot is not None:
                coalescer.drop_pending()
                hub.broadcast(ev.snapshot)
            elif ev.bars is not None and (ev.changes.rows or ev.changes.bars):
                coalescer.push_rows(ev.changes.rows, ev.bars)
            if ev.snapshot is None and ev.changes.lines:
                coalescer.push_lines(ev.changes.lines)
            for event in ev.changes.events:
                coalescer.push_event(event)
                task = asyncio.create_task(notifier.handle(event))
                notify_tasks.add(task)
                task.add_done_callback(notify_tasks.discard)
        elif isinstance(ev, SessionEnded):
            coalescer.flush()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not cfg.server.token:
            raise ConfigError("config.toml has no [server] token; run fmon setup first")
        prime()
        queue = bus.subscribe()
        tasks: list[asyncio.Task[None]] = []
        advertiser = Advertiser(cfg.server.port, __version__)
        await advertiser.start()
        print_pairing(cfg)
        hub.start()
        tasks.append(asyncio.create_task(supervise_manager()))
        tasks.append(asyncio.create_task(pump(queue)))
        recorder: RecorderClient | None = None
        if record is not None:
            recorder = RecorderClient(record)
            hub.clients.add(recorder.client)
            tasks.append(recorder.start())
        app.state.background_tasks = tasks
        try:
            yield
        finally:
            for task in reversed(tasks):
                task.cancel()
            for task in list(notify_tasks):
                task.cancel()
            await asyncio.gather(*tasks, *notify_tasks, return_exceptions=True)
            await advertiser.stop()
            if hub.idle is not None:
                hub.idle.cancel()
            coalescer.close()
            bus.unsubscribe(queue)
            if recorder is not None:
                hub.clients.discard(recorder.client)
                recorder.close()

    app.router.lifespan_context = lifespan
    return app
