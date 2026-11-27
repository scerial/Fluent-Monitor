from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from fluentmon.config import Config
from fluentmon.transcript.manager import CX_NAME, case_name

log = logging.getLogger("fluentmon.launcher")

HOST_WAIT_S = 30.0
HOST_POLL_S = 0.2
CLOCK_SLACK_S = 2.0
JOURNAL = '/file/start-transcript "{console}"\n/file/read-case-data "{case}"\n'


@dataclass(frozen=True)
class RunSpec:
    case: Path
    cores: int
    fluent_exe: Path
    version: str = "3ddp"
    extra_args: tuple[str, ...] = ()


def _unique_folder(runs: Path, base: str) -> Path:
    candidate = runs / base
    counter = 2
    while candidate.exists():
        candidate = runs / f"{base}-{counter}"
        counter += 1
    return candidate


def prepare_run(cfg: Config, spec: RunSpec, now: datetime | None = None) -> Path:
    moment = now if now is not None else datetime.now().astimezone()
    runs = Path(cfg.server.runs_dir)
    runs.mkdir(parents=True, exist_ok=True)
    folder = _unique_folder(runs, f"{moment:%Y%m%d-%H%M%S}-{case_name(str(spec.case))}")
    folder.mkdir()
    journal = JOURNAL.format(
        console=(folder / "console.trn").as_posix(),
        case=Path(spec.case).as_posix(),
    )
    (folder / "start.jou").write_text(journal, encoding="utf-8")
    return folder


def _resolve_fluent(exe: Path) -> str | None:
    if exe.is_file():
        return str(exe)
    return shutil.which(str(exe))


def _write_json_atomic(path: Path, data: dict) -> None:
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _find_host(parent_pid: int, since: float) -> psutil.Process | None:
    for process in psutil.process_iter(["name"]):
        if not CX_NAME.match((process.info.get("name") or "").lower()):
            continue
        try:
            if process.ppid() == parent_pid and process.create_time() >= since - CLOCK_SLACK_S:
                return process
        except psutil.Error:
            continue
    return None


def _wait_for_host(launcher: subprocess.Popen, since: float) -> psutil.Process | None:
    deadline = time.monotonic() + HOST_WAIT_S
    while True:
        exited = launcher.poll() is not None
        host = _find_host(launcher.pid, since)
        if host is not None or exited or time.monotonic() >= deadline:
            return host
        time.sleep(HOST_POLL_S)


def launch(cfg: Config, spec: RunSpec) -> int:
    fluent = _resolve_fluent(spec.fluent_exe)
    if fluent is None:
        log.error("Fluent executable not found: %s", spec.fluent_exe)
        return 2
    now = datetime.now().astimezone()
    folder = prepare_run(cfg, spec, now)
    env = os.environ.copy()
    env["I_MPI_PIN_PROCESSOR_LIST"] = ",".join(str(cpu) for cpu in cfg.meters.cpus)
    command = [fluent, spec.version, f"-t{spec.cores}", "-i", "start.jou", *spec.extra_args]
    since = time.time()
    process = subprocess.Popen(
        command,
        cwd=folder,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    host = _wait_for_host(process, since)
    _write_json_atomic(
        folder / "session.json",
        {
            "case": str(spec.case),
            "launched": now.isoformat(timespec="seconds"),
            "pid": process.pid if host is None else host.pid,
            "cores": spec.cores,
        },
    )
    if host is None:
        return process.wait()
    try:
        host_code = host.wait()
    except psutil.Error:
        host_code = None
    launcher_code = process.wait()
    return launcher_code if host_code is None else host_code
