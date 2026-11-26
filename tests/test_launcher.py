from __future__ import annotations

import json
import logging
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluentmon.config import Config
from fluentmon.launcher import RunSpec, launch, prepare_run

STAMP = datetime(2026, 11, 25, 10, 41, 7, tzinfo=timezone(timedelta(hours=9)))
STUB = """#!{python}
import os
import sys
with open("args.txt", "w", encoding="utf-8") as handle:
    handle.write("\\n".join(sys.argv[1:]) + "\\n")
    handle.write(os.environ.get("I_MPI_PIN_PROCESSOR_LIST", "") + "\\n")
sys.exit(3)
"""


def make_cfg(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.server.runs_dir = str(tmp_path / "runs")
    return cfg


def make_stub(tmp_path: Path) -> Path:
    path = tmp_path / "fluent_stub"
    path.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_run_folder_uses_case_name(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    spec = RunSpec(case=Path("D:/cfd/duct.cas.h5"), cores=4, fluent_exe=Path("fluent"))
    folder = prepare_run(cfg, spec, STAMP)
    assert folder.name == "20261125-104107-duct"
    assert folder.parent == tmp_path / "runs"


def test_same_second_clash_appends_suffix(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    spec = RunSpec(case=Path("duct.cas.h5"), cores=4, fluent_exe=Path("fluent"))
    first = prepare_run(cfg, spec, STAMP)
    second = prepare_run(cfg, spec, STAMP)
    assert first.name == "20261125-104107-duct"
    assert second.name == "20261125-104107-duct-2"


def test_start_jou_matches_section_one_journal(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    spec = RunSpec(case=Path("D:/cfd/case.cas.h5"), cores=4, fluent_exe=Path("fluent"))
    folder = prepare_run(cfg, spec, STAMP)
    expected = (
        f'/file/start-transcript "{(folder / "console.trn").as_posix()}"\n'
        '/file/read-case-data "D:/cfd/case.cas.h5"\n'
    )
    assert (folder / "start.jou").read_text(encoding="utf-8") == expected


def test_spaces_and_korean_in_case_path(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    spec = RunSpec(case=Path("D:/작업 폴더/케이스 1.cas.h5"), cores=2, fluent_exe=Path("fluent"))
    folder = prepare_run(cfg, spec, STAMP)
    assert folder.name == "20261125-104107-케이스 1"
    journal = (folder / "start.jou").read_text(encoding="utf-8")
    assert 'read-case-data "D:/작업 폴더/케이스 1.cas.h5"' in journal


def test_launch_writes_session_and_returns_exit_code(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    stub = make_stub(tmp_path)
    spec = RunSpec(case=Path("D:/cfd/duct.cas.h5"), cores=4, fluent_exe=stub)
    code = launch(cfg, spec)
    assert code == 3
    run = next((tmp_path / "runs").iterdir())
    session = json.loads((run / "session.json").read_text(encoding="utf-8"))
    assert set(session) == {"case", "launched", "pid", "cores"}
    assert session["pid"] > 0
    assert session["cores"] == 4
    assert session["case"] == "D:/cfd/duct.cas.h5"
    args = (run / "args.txt").read_text(encoding="utf-8").splitlines()
    assert args == ["3ddp", "-t4", "-i", "start.jou", "0,4,8,12"]
    assert "-g" not in args
    assert run.name.endswith("-duct")


def test_extra_args_follow_the_fixed_arguments(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    stub = make_stub(tmp_path)
    spec = RunSpec(case=Path("duct.cas.h5"), cores=2, fluent_exe=stub, extra_args=("-meshing",))
    assert launch(cfg, spec) == 3
    run = next((tmp_path / "runs").iterdir())
    args = (run / "args.txt").read_text(encoding="utf-8").splitlines()
    assert args[:4] == ["3ddp", "-t2", "-i", "start.jou"]
    assert args[4] == "-meshing"


def test_missing_fluent_is_refused_before_creating_a_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = make_cfg(tmp_path)
    spec = RunSpec(case=Path("duct.cas.h5"), cores=2, fluent_exe=tmp_path / "no-such-fluent")
    with caplog.at_level(logging.ERROR, logger="fluentmon.launcher"):
        assert launch(cfg, spec) == 2
    assert "not found" in caplog.text
    assert not (tmp_path / "runs").exists()
