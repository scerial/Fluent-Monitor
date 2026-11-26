from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPLAY_PATH = Path(__file__).resolve().parents[1] / "tools" / "replay.py"


def load_replay() -> ModuleType:
    spec = importlib.util.spec_from_file_location("replay_tool", REPLAY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay = load_replay()

FIXTURE_TEXT = (
    "Flow time = 1.2000e-02s, time step = 120\r\n"
    "  iter  continuity  k  time/iter\n"
    "   118  2.3156e-03  2.0981e-04  0:01:12  382\n"
    "Warning: 한글 경로 확인\n"
    "   119  2.1000e-03  2.0000e-04  0:01:11  381\n"
    "   120  2.0000e-03  1.9000e-04  0:01:10  380\n"
    "880 more time steps\n"
)


@pytest.fixture
def fixture_path(tmp_path: Path) -> Path:
    path = tmp_path / "fixture" / "transient.trn"
    path.parent.mkdir()
    path.write_bytes(FIXTURE_TEXT.encode("utf-8"))
    return path


def test_burst_is_byte_identical(tmp_path: Path, fixture_path: Path) -> None:
    out = tmp_path / "out" / "replay.trn"
    assert replay.main([str(fixture_path), "--burst", "--out", str(out)]) == 0
    assert out.read_bytes() == fixture_path.read_bytes()


def test_ticks_concatenate_to_fixture_and_start_at_data(fixture_path: Path) -> None:
    lines = fixture_path.read_bytes().splitlines(keepends=True)
    groups = replay.ticks(lines)
    assert b"".join(b"".join(group) for group in groups) == fixture_path.read_bytes()
    for group in groups[1:]:
        assert replay.is_data_line(group[0])
    assert len(groups) == 4


def test_fast_ticks_write_full_fixture(tmp_path: Path, fixture_path: Path) -> None:
    out = tmp_path / "replay.trn"
    assert replay.main([str(fixture_path), "--ips", "1000000", "--out", str(out)]) == 0
    assert out.read_bytes() == fixture_path.read_bytes()


def test_no_data_lines_written_in_one_tick(tmp_path: Path) -> None:
    fixture = tmp_path / "plain.trn"
    fixture.write_bytes(b"alpha\nbeta\n")
    assert replay.ticks(fixture.read_bytes().splitlines(keepends=True)) == [
        [b"alpha\n", b"beta\n"]
    ]
    out = tmp_path / "plain_out.trn"
    assert replay.main([str(fixture), "--ips", "1000000", "--out", str(out)]) == 0
    assert out.read_bytes() == b"alpha\nbeta\n"


def test_missing_fixture_returns_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert replay.main([str(tmp_path / "absent.trn"), "--out", str(tmp_path / "x.trn")]) == 2
    assert "fixture not found" in capsys.readouterr().err


def test_output_folder_is_created(tmp_path: Path, fixture_path: Path) -> None:
    out = tmp_path / "deep" / "folder" / "replay.trn"
    assert replay.main([str(fixture_path), "--burst", "--out", str(out)]) == 0
    assert out.read_bytes() == fixture_path.read_bytes()


def test_existing_output_is_truncated(tmp_path: Path, fixture_path: Path) -> None:
    out = tmp_path / "replay.trn"
    out.write_bytes(b"old content that is much longer than the fixture " * 20)
    assert replay.main([str(fixture_path), "--burst", "--out", str(out)]) == 0
    assert out.read_bytes() == fixture_path.read_bytes()


def test_non_positive_rate_is_refused(tmp_path: Path, fixture_path: Path) -> None:
    assert replay.main([str(fixture_path), "--ips", "0", "--out", str(tmp_path / "x.trn")]) == 2
