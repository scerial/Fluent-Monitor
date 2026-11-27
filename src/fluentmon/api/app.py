from __future__ import annotations

import asyncio
import csv
import io
import math
import os
import secrets
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import psutil
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from fluentmon import __version__
from fluentmon.api.hub import Client, Hub, client_writer
from fluentmon.config import Config, save_config
from fluentmon.system.sampler import Sampler
from fluentmon.transcript.assembler import LineAssembler
from fluentmon.transcript.manager import SessionManager
from fluentmon.transcript.parser import LineEv, Parser

CLOSE_UNAUTHORIZED = 4401


class ConfigUpdate(BaseModel):
    target: float | None = None
    cpus: list[int] | None = None
    exclude: list[str] | None = None
    pkg_max_w: float | None = None


def default_config_path() -> Path:
    program_data = os.environ.get("ProgramData")
    if program_data:
        return Path(program_data) / "FluentMon" / "config.toml"
    return Path.cwd() / "config.toml"


def hello_message(cfg: Config, manager: SessionManager) -> dict[str, Any]:
    current = manager.current()
    return {
        "t": "hello",
        "proto": 1,
        "server": socket.gethostname(),
        "version": __version__,
        "session": current.session_id if current is not None else None,
        "thermal": {
            "base_c": cfg.meters.temp_base_c,
            "warn_c": cfg.meters.temp_warn_c,
            "max_c": cfg.meters.tjmax_c,
        },
    }


def snapshot_message(manager: SessionManager) -> dict[str, Any] | None:
    current = manager.current()
    if current is None:
        return None
    return current.snapshot_msg()


def reread_console(path: Path, before: int, n: int) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    start = max(1, before - n)
    assembler = LineAssembler()
    parser = Parser()
    lines: list[str] = []
    kinds: list[str] = []
    number = 0
    for raw in assembler.feed(path.read_bytes()):
        number += 1
        event = parser.feed(raw)[0]
        if isinstance(event, LineEv) and start <= number < before:
            lines.append(event.text)
            kinds.append(event.kind)
    return {"t": "con", "first": start, "lines": lines, "kinds": "".join(kinds)}


def _bearer(header: str | None) -> str | None:
    if header and header.startswith("Bearer "):
        return header[len("Bearer ") :]
    return None


def _token_ok(presented: str | None, expected: str) -> bool:
    if not expected or presented is None:
        return False
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _csv_chunks(columns: list[str], iters: list[int], values: list[list[float]]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def take() -> str:
        text = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()
        return text

    writer.writerow(["iter", *columns])
    yield take()
    for index, it in enumerate(iters):
        row = [it]
        for column in values:
            value = column[index]
            row.append("" if math.isnan(value) else value)
        writer.writerow(row)
        yield take()


def create_app(cfg: Config, hub: Hub, manager: SessionManager, sampler: Sampler) -> FastAPI:
    app = FastAPI()

    def guard(request: Request) -> None:
        if not _token_ok(_bearer(request.headers.get("authorization")), cfg.server.token):
            raise HTTPException(status_code=401, detail="unauthorized")

    def current_config() -> dict[str, Any]:
        return {
            "target": cfg.convergence.target,
            "cpus": list(cfg.meters.cpus),
            "exclude": list(cfg.convergence.exclude),
            "pkg_max_w": cfg.meters.pkg_max_w,
        }

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "lhm": sampler.lhm_ok}

    @app.get("/api/sessions", dependencies=[Depends(guard)])
    async def sessions() -> list[dict[str, Any]]:
        return [
            {
                "id": state.session_id,
                "case": state.case,
                "mode": state.mode,
                "status": state.status,
                "started": state.started.isoformat(timespec="seconds"),
            }
            for state in manager.sessions()
        ]

    @app.get("/api/sessions/{session_id}/residuals.csv", dependencies=[Depends(guard)])
    async def residuals(session_id: str) -> StreamingResponse:
        state = next((s for s in manager.sessions() if s.session_id == session_id), None)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown session")
        columns = list(state.columns)
        iters = list(state.iters)
        values = [list(column) for column in state.values]
        return StreamingResponse(_csv_chunks(columns, iters, values), media_type="text/csv")

    @app.put("/api/config", dependencies=[Depends(guard)])
    async def put_config(update: ConfigUpdate) -> dict[str, Any]:
        if update.target is not None and not update.target > 0:
            raise HTTPException(status_code=422, detail="target must be above 0")
        if update.pkg_max_w is not None and not update.pkg_max_w > 0:
            raise HTTPException(status_code=422, detail="pkg_max_w must be above 0")
        if update.cpus is not None:
            if any(cpu < 0 for cpu in update.cpus):
                raise HTTPException(status_code=422, detail="cpus must be non-negative")
            present = psutil.cpu_count() or 0
            if any(cpu >= present for cpu in update.cpus):
                raise HTTPException(status_code=422, detail="cpus id not present on this machine")
        changed = any(
            value is not None
            for value in (update.target, update.cpus, update.exclude, update.pkg_max_w)
        )
        if not changed:
            return current_config()
        if update.target is not None:
            cfg.convergence.target = update.target
        if update.exclude is not None:
            cfg.convergence.exclude = list(update.exclude)
        if update.cpus is not None:
            cfg.meters.cpus = list(update.cpus)
            sampler.set_cpus(cfg.meters.cpus)
        if update.pkg_max_w is not None:
            cfg.meters.pkg_max_w = update.pkg_max_w
            sampler.set_pkg_max_w(update.pkg_max_w)
        current = manager.current()
        if current is not None and (update.target is not None or update.exclude is not None):
            current.set_options(update.target, update.exclude)
        save_config(cfg, cfg.source if cfg.source is not None else default_config_path())
        hub.send(hello_message(cfg, manager))
        snapshot = snapshot_message(manager)
        if snapshot is not None:
            hub.broadcast(snapshot)
        return current_config()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        presented = ws.query_params.get("token") or _bearer(ws.headers.get("authorization"))
        if not _token_ok(presented, cfg.server.token):
            await ws.close(code=CLOSE_UNAUTHORIZED)
            return
        client = Client(ws)
        hub.send_to(client, hello_message(cfg, manager))
        snapshot = snapshot_message(manager)
        if snapshot is not None:
            hub.send_to(client, snapshot)
        hub.clients.add(client)
        writer = asyncio.create_task(client_writer(client))
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if not raw:
                    continue
                try:
                    body = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    continue
                if not isinstance(body, dict):
                    continue
                kind = body.get("t")
                if kind == "sub":
                    console = body.get("console")
                    if isinstance(console, bool):
                        client.console = console
                elif kind == "con_back":
                    before = body.get("before")
                    n = body.get("n")
                    if isinstance(before, int) and isinstance(n, int):
                        _answer_con_back(client, before, n)
        finally:
            hub.clients.discard(client)
            writer.cancel()

    def _answer_con_back(client: Client, before: int, n: int) -> None:
        current = manager.current()
        if current is None:
            return
        reply = current.console_range(before, n)
        if reply is None:
            path = manager.transcript_path(current.session_id)
            if path is None:
                return
            reply = reread_console(path, before, n)
        if reply is not None:
            hub.send_to(client, reply)

    return app
