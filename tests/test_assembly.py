from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import orjson
import pytest
from fastapi.testclient import TestClient

import fluentmon.app as app_module
from fluentmon.app import build_app
from fluentmon.config import Config, ConfigError
from fluentmon.transcript.manager import SessionManager

TOKEN = "x" * 32
RUN = "20261125-1041_duct"
HEADER = "  iter  continuity  x-velocity  time/iter"


def transcript_lines() -> list[str]:
    lines = [HEADER]
    for it in range(1, 6):
        lines.append(f"{it:6d}  {10 ** (-0.5 * it):.4e}  {10 ** (-0.6 * it):.4e}  0:00:10  {20 - it}")
    lines.append("! 5 solution is converged")
    return lines


def make_cfg(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.server.token = TOKEN
    cfg.server.runs_dir = str(tmp_path / "runs")
    cfg.server.watch_dir = str(tmp_path / "watch")
    cfg.meters.cpus = [0]
    cfg.meters.lhm_url = "http://127.0.0.1:9"
    cfg.source = tmp_path / "config.toml"
    return cfg


def start_run(cfg: Config) -> Path:
    folder = Path(cfg.server.runs_dir) / RUN
    folder.mkdir(parents=True)
    console = folder / "console.trn"
    console.write_text("", encoding="utf-8")
    meta = {"case": "C:/cases/duct.cas.h5", "launched": "2026-11-25T10:41:07+09:00", "pid": None, "cores": 4}
    (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    return console


def take(ws: Any, stop: str) -> list[dict]:
    collected: list[dict] = []
    while True:
        msg = orjson.loads(ws.receive_bytes())
        if msg["t"] == "sys":
            continue
        collected.append(msg)
        if msg["t"] == stop or (msg["t"] == "event" and msg.get("kind") == stop):
            return collected


def append_line(console: Path, line: str) -> None:
    with console.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
    time.sleep(0.05)


def test_hello_snapshot_resid_bars_then_converged(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = build_app(cfg)
    lines = transcript_lines()
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            assert take(ws, "hello")[0]["t"] == "hello"
            console = start_run(cfg)
            assert take(ws, "snapshot")[-1]["t"] == "snapshot"
            append_line(console, lines[0])
            assert take(ws, "snapshot")[-1]["t"] == "snapshot"
            append_line(console, lines[1])
            live = take(ws, "bars")
            kinds = [msg["t"] for msg in live if msg["t"] != "con"]
            assert kinds == ["resid", "bars"]
            for line in lines[2:-1]:
                append_line(console, line)
            append_line(console, lines[-1])
            tail = take(ws, "converged")
            assert tail[-1]["kind"] == "converged"
            assert tail[-1]["iter"] == 5


def test_recorder_writes_valid_json_lines_with_increasing_at(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    record = tmp_path / "record.jsonl"
    app = build_app(cfg, record)
    with TestClient(app) as client:
        console = start_run(cfg)
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            take(ws, "hello")
            take(ws, "snapshot")
        for line in transcript_lines():
            append_line(console, line)
        time.sleep(0.2)
    entries = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert entries
    assert all(set(entry) == {"at", "msg"} for entry in entries)
    stamps = [entry["at"] for entry in entries]
    assert stamps == sorted(stamps)


def test_empty_token_refuses_to_start(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cfg.server.token = ""
    app = build_app(cfg)
    with pytest.raises(ConfigError):
        with TestClient(app):
            pass


def test_cancelled_lifespan_leaves_no_running_tasks(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    app = build_app(cfg)
    with TestClient(app):
        tasks = list(app.state.background_tasks)
        assert tasks
    assert all(task.done() for task in tasks)


def test_failing_manager_is_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(app_module, "MANAGER_RESTART_S", 0.05)
    calls: list[int] = []
    original_run = SessionManager.run

    async def flaky(self: SessionManager) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("manager broke")
        await original_run(self)

    monkeypatch.setattr(SessionManager, "run", flaky)
    app = build_app(cfg)
    with caplog.at_level(logging.ERROR, logger="fluentmon.app"):
        with TestClient(app):
            time.sleep(0.3)
            assert len(calls) >= 2
    assert "restarting" in caplog.text


def test_serve_refuses_empty_token_with_exit_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fluentmon.cli import main
    from fluentmon.config import save_config

    cfg = make_cfg(tmp_path)
    cfg.server.token = ""
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert main(["serve", "--config", str(path)]) == 2
    assert "token" in capsys.readouterr().err
