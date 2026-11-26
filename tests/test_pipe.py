from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from fluentmon.transcript.pipe import PipeSource


def test_three_lines_copied_byte_for_byte(tmp_path: Path) -> None:
    data = b"line one\nline two\r\nline three\n"
    out = tmp_path / "console.trn"
    PipeSource(io.BytesIO(data), out).run()
    assert out.read_bytes() == data


def test_closed_stream_at_once_creates_empty_file(tmp_path: Path) -> None:
    out = tmp_path / "console.trn"
    PipeSource(io.BytesIO(b""), out).run()
    assert out.read_bytes() == b""


def test_write_error_is_logged_and_pipe_still_drained(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stream = io.BytesIO(b"x" * 200_000)
    bad_out = tmp_path
    with caplog.at_level(logging.ERROR, logger="fluentmon.transcript.pipe"):
        PipeSource(stream, bad_out).run()
    assert stream.tell() == 200_000
    assert any("pipe copy" in record.getMessage() or "cannot open" in record.getMessage() for record in caplog.records)


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full to force a write error")
def test_mid_copy_write_error_is_logged_and_drained(caplog: pytest.LogCaptureFixture) -> None:
    stream = io.BytesIO(b"y" * 200_000)
    with caplog.at_level(logging.ERROR, logger="fluentmon.transcript.pipe"):
        PipeSource(stream, Path("/dev/full")).run()
    assert stream.tell() == 200_000
    assert any("still draining" in record.getMessage() for record in caplog.records)
