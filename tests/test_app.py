from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fluentmon import __version__
from fluentmon.api.app import create_app
from fluentmon.api.hub import Hub, PcPoller
from fluentmon.config import Config, load_config
from fluentmon.model.session import SessionState
from fluentmon.system.cpu import prime
from fluentmon.system.memory import FluentTree
from fluentmon.system.power import LhmClient
from fluentmon.system.sampler import Sampler
from fluentmon.transcript.parser import IterEv, LineEv, Parser

TOKEN = "t" * 32
FIXED = datetime(2026, 11, 25, 10, 41, 7, tzinfo=timezone(timedelta(hours=9)))
HEADER = "  iter  continuity  k  time/iter"


class StubSampler:
    def __init__(self, lhm: bool = True) -> None:
        self._lhm = lhm
        self.cpus: list[int] | None = None
        self.pkg: float | None = None

    @property
    def lhm_ok(self) -> bool:
        return self._lhm

    def set_cpus(self, ids: list[int]) -> None:
        self.cpus = list(ids)

    def set_pkg_max_w(self, w: float) -> None:
        self.pkg = w


class StubManager:
    def __init__(self, sessions: list[SessionState], path: Path | None = None) -> None:
        self._sessions = sessions
        self._path = path

    def current(self) -> SessionState | None:
        return self._sessions[0] if self._sessions else None

    def sessions(self) -> list[SessionState]:
        return list(self._sessions)

    def transcript_path(self, session_id: str) -> Path | None:
        if self._path is not None and self.current() is not None and self.current().session_id == session_id:
            return self._path
        return None


def make_state(cfg: Config, session_id: str = "s1") -> SessionState:
    state = SessionState(cfg, session_id, "duct", FIXED, clock=lambda: FIXED)
    for ev in Parser().feed(HEADER):
        state.apply(ev, 0.0)
    return state


def feed_rows(state: SessionState, count: int, start: int = 1) -> None:
    for it in range(start, start + count):
        state.apply(IterEv(it, (1.0 / it, 0.5), 0, 1000 - it), float(it))


def make_hub() -> Hub:
    holder: dict[str, Hub] = {}
    poller = PcPoller(lambda msg: holder["hub"].send(msg), lambda: {"t": "sys"}, min_gap_s=0.0)
    hub = Hub(poller, idle_s=60.0)
    holder["hub"] = hub
    return hub


def base_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.server.token = TOKEN
    cfg.source = tmp_path / "config.toml"
    return cfg


def build(cfg: Config, manager: Any, sampler: Any | None = None) -> tuple[TestClient, Hub]:
    hub = make_hub()
    app = create_app(cfg, hub, manager, sampler or StubSampler())
    return TestClient(app), hub


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def recv(ws: Any) -> dict:
    while True:
        msg = orjson.loads(ws.receive_bytes())
        if msg["t"] != "sys":
            return msg


def emit(client: TestClient, hub: Hub, msg: dict) -> None:
    async def send() -> None:
        hub.broadcast(msg)

    client.portal.call(send)


def test_wrong_token_is_closed_with_4401() -> None:
    cfg = base_config(Path("/tmp"))
    client, _ = build(cfg, StubManager([]))
    with client:
        with pytest.raises(WebSocketDisconnect) as info:
            with client.websocket_connect("/ws?token=wrong") as ws:
                ws.receive_bytes()
        assert info.value.code == 4401


def test_missing_token_is_closed_with_4401() -> None:
    cfg = base_config(Path("/tmp"))
    client, _ = build(cfg, StubManager([]))
    with client:
        with pytest.raises(WebSocketDisconnect) as info:
            with client.websocket_connect("/ws") as ws:
                ws.receive_bytes()
        assert info.value.code == 4401


def test_hello_then_snapshot_with_exact_keys(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    feed_rows(state, 3)
    client, _ = build(cfg, StubManager([state]))
    with client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            hello = orjson.loads(ws.receive_bytes())
            snapshot = orjson.loads(ws.receive_bytes())
    assert set(hello) == {"t", "proto", "server", "version", "session", "thermal"}
    assert hello["session"] == "s1"
    assert hello["version"] == __version__
    assert hello["thermal"] == {"base_c": 20.0, "warn_c": 70.0, "max_c": 89.0}
    assert snapshot["t"] == "snapshot"
    assert snapshot["iters"] == [1, 2, 3]


def test_header_auth_is_accepted(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    client, _ = build(cfg, StubManager([state]))
    with client:
        with client.websocket_connect("/ws", headers=auth()) as ws:
            assert orjson.loads(ws.receive_bytes())["t"] == "hello"


def test_no_session_gets_hello_only(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, hub = build(cfg, StubManager([]))
    with client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            hello = orjson.loads(ws.receive_bytes())
            assert hello["session"] is None
            emit(client, hub, {"t": "resid", "rows": []})
            assert recv(ws)["t"] == "resid"


def test_sub_console_off_stops_con(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    client, hub = build(cfg, StubManager([state]))
    with client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            recv(ws)
            recv(ws)
            ws.send_text(orjson.dumps({"t": "sub", "console": False}).decode())
            emit(client, hub, {"t": "con", "first": 1, "lines": ["x"], "kinds": "n"})
            emit(client, hub, {"t": "resid", "rows": []})
            assert recv(ws)["t"] == "resid"


def test_con_back_returns_requested_lines(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    for index in range(20):
        state.apply(LineEv(f"line {index + 1}", "n"), 0.0)
    client, _ = build(cfg, StubManager([state]))
    with client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            recv(ws)
            recv(ws)
            ws.send_text(orjson.dumps({"t": "con_back", "before": 12, "n": 3}).decode())
            reply = recv(ws)
    assert reply == {
        "t": "con",
        "first": 9,
        "lines": ["line 8", "line 9", "line 10"],
        "kinds": "nnn",
    }


def test_con_back_older_than_deque_rereads_transcript(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    transcript = tmp_path / "console.trn"
    transcript.write_text("".join(f"row {n}\n" for n in range(1, 25_001)), encoding="utf-8")
    for index in range(25_000):
        state.apply(LineEv(f"row {index + 1}", "n"), 0.0)
    client, _ = build(cfg, StubManager([state], transcript))
    with client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            recv(ws)
            recv(ws)
            ws.send_text(orjson.dumps({"t": "con_back", "before": 4, "n": 2}).decode())
            reply = recv(ws)
    assert reply["first"] == 2
    assert reply["lines"] == ["row 2", "row 3"]


def test_row_appears_once_across_snapshot_and_live(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    feed_rows(state, 4)
    client, hub = build(cfg, StubManager([state]))
    with client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            recv(ws)
            snapshot = recv(ws)
            emit(client, hub, {"t": "resid", "rows": [[5, [0.2, 0.5]]]})
            live = recv(ws)
    assert snapshot["iters"] == [1, 2, 3, 4]
    assert live["rows"] == [[5, [0.2, 0.5]]]


def test_bad_json_from_client_is_ignored(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, hub = build(cfg, StubManager([]))
    with client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            recv(ws)
            ws.send_text("{not json")
            ws.send_text(orjson.dumps({"t": "ping"}).decode())
            emit(client, hub, {"t": "resid", "rows": []})
            assert recv(ws)["t"] == "resid"


def test_health_is_open_and_exact(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, _ = build(cfg, StubManager([]), StubSampler(lhm=False))
    with client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": __version__, "lhm": False}


def test_other_endpoints_need_token(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, _ = build(cfg, StubManager([]))
    with client:
        assert client.get("/api/sessions").status_code == 401
        assert client.get("/api/sessions", headers=auth("nope")).status_code == 401
        assert client.put("/api/config", json={}).status_code == 401


def test_sessions_list(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    client, _ = build(cfg, StubManager([state]))
    with client:
        response = client.get("/api/sessions", headers=auth())
    assert response.json() == [
        {
            "id": "s1",
            "case": "duct",
            "mode": "steady",
            "status": "running",
            "started": FIXED.isoformat(timespec="seconds"),
        }
    ]


def test_residuals_csv_header_rows_and_nan(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    state.apply(IterEv(1, (1.0, math.nan), 0, 3), 0.0)
    state.apply(IterEv(2, (0.5, 0.25), 0, 2), 1.0)
    state.apply(IterEv(3, (0.25, 0.125), 0, 1), 2.0)
    client, _ = build(cfg, StubManager([state]))
    with client:
        response = client.get("/api/sessions/s1/residuals.csv", headers=auth())
    assert response.status_code == 200
    lines = response.text.strip().splitlines()
    assert lines[0] == "iter,continuity,k"
    assert lines[1] == "1,1.0,"
    assert len(lines) == 4


def test_unknown_session_csv_is_404(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, _ = build(cfg, StubManager([]))
    with client:
        assert client.get("/api/sessions/nope/residuals.csv", headers=auth()).status_code == 404


def test_put_changes_target_and_writes_file(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    state = make_state(cfg)
    client, _ = build(cfg, StubManager([state]))
    with client:
        response = client.put("/api/config", headers=auth(), json={"target": 1e-3})
    assert response.status_code == 200
    assert response.json()["target"] == 1e-3
    assert load_config(cfg.source).convergence.target == 1e-3
    state.apply(IterEv(1, (1.0, 1e-2), 0, 5), 0.0)
    assert state.bars_msg()["conv"]["of"] == pytest.approx(3.0)


def test_put_pkg_max_w_reaches_next_sys(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    cfg.meters.cpus = [0]
    prime()
    lhm = LhmClient("http://127.0.0.1:1", None, [], None, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    sampler = Sampler(cfg, lhm, FluentTree(None))
    client, _ = build(cfg, StubManager([]), sampler)
    with client:
        response = client.put("/api/config", headers=auth(), json={"pkg_max_w": 140})
    assert response.status_code == 200
    sampler.set_cpus([0])
    assert sampler()["pkg_max_w"] == 140.0
    assert load_config(cfg.source).meters.pkg_max_w == 140.0


def test_put_pkg_max_w_zero_is_422_and_changes_nothing(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, _ = build(cfg, StubManager([]))
    with client:
        response = client.put("/api/config", headers=auth(), json={"pkg_max_w": 0})
    assert response.status_code == 422
    assert cfg.meters.pkg_max_w == 162.0
    assert not cfg.source.exists()


def test_put_missing_cpu_is_422(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, _ = build(cfg, StubManager([]))
    with client:
        response = client.put("/api/config", headers=auth(), json={"cpus": [10_000]})
    assert response.status_code == 422
    assert cfg.meters.cpus == [0, 4, 8, 12]


def test_put_without_keys_returns_current_and_writes_nothing(tmp_path: Path) -> None:
    cfg = base_config(tmp_path)
    client, _ = build(cfg, StubManager([]))
    with client:
        response = client.put("/api/config", headers=auth(), json={})
    assert response.status_code == 200
    assert response.json() == {
        "target": 1e-6,
        "cpus": [0, 4, 8, 12],
        "exclude": [],
        "pkg_max_w": 162.0,
    }
    assert not cfg.source.exists()
