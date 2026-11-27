from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fluentmon import __version__
from fluentmon.cli import main


def test_version_prints_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


@pytest.mark.parametrize(
    ("command", "target", "argv"),
    [
        ("setup", "run_setup", ["setup", "--yes", "--dry-run"]),
        ("doctor", "run_doctor", ["doctor", "--json"]),
        ("uninstall", "run_uninstall", ["uninstall", "--purge"]),
    ],
)
def test_installer_subcommands_reach_commands(
    command: str, target: str, argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from fluentmon import cli
    from fluentmon.setup import winbackend

    sentinel = object()
    calls: list[tuple] = []

    def record(*args):
        calls.append(args)
        return 7

    monkeypatch.setattr(winbackend, "WindowsBackend", lambda: sentinel)
    monkeypatch.setattr(cli, target, record)
    assert main(argv) == 7
    assert len(calls) == 1
    args = calls[0]
    assert args[0] == argv[1:]
    assert args[1] is sentinel
    if command == "doctor":
        assert args[2] is sys.stdout
    else:
        assert args[2] is input
        assert args[3] is sys.stdout


def test_launch_with_missing_fluent_returns_two(tmp_path: Path) -> None:
    code = main(
        [
            "launch",
            str(tmp_path / "duct.cas.h5"),
            "--cores",
            "2",
            "--fluent-exe",
            str(tmp_path / "missing"),
            "--config",
            str(tmp_path / "absent.toml"),
        ]
    )
    assert code == 2


def test_launch_runs_stub_and_returns_its_code(tmp_path: Path) -> None:
    import stat
    import sys

    stub = tmp_path / "fluent_stub"
    stub.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    config = tmp_path / "config.toml"
    config.write_text(f'[server]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n', encoding="utf-8")
    code = main(
        [
            "launch",
            str(tmp_path / "duct.cas.h5"),
            "--cores",
            "2",
            "--fluent-exe",
            str(stub),
            "--config",
            str(config),
        ]
    )
    assert code == 3
    assert any((tmp_path / "runs").iterdir())


def test_serve_without_streams_logs_to_server_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from fluentmon import cli

    program_data = tmp_path / "ProgramData" / "FluentMon"
    config = tmp_path / "config.toml"
    config.write_text('[server]\ntoken = "abc"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "default_config_path", lambda: program_data / "config.toml")
    monkeypatch.setattr(cli, "build_app", lambda cfg, record: object())
    def fake_run(*args: object, **kwargs: object) -> None:
        logging.getLogger("fluentmon.probe").info("server started")
        logging.getLogger("httpx").info("HTTP Request: GET data.json")

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(logging.root, "handlers", [])
    monkeypatch.setattr(logging.root, "level", logging.WARNING)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["serve", "--config", str(config)]) == 0
    log_file = program_data / "logs" / "server.log"
    text = log_file.read_text(encoding="utf-8")
    assert "server started" in text
    assert "HTTP Request" not in text
    assert "Logging error" not in text
    sys.stdout.close()
