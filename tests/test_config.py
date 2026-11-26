from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fluentmon.config import (
    Config,
    ConfigError,
    find_config,
    load_config,
    save_config,
)

SAMPLE = """
[server]
port = 8765
token = "generate-with-secrets.token_urlsafe(24)"
watch_dir = "C:/FluentMon/transcripts"
runs_dir = "C:/FluentMon/runs"

[convergence]
target = 1e-6
exclude = []
extra_residual_names = []

[meters]
cpus = [0, 4, 8, 12]
pin_ranks = false
lhm_url = "http://127.0.0.1:8085"
power_sensor = "AMD Ryzen 7 7800X3D/Powers/Package"
pkg_max_w = 162
clock_sensors = ["Core #1 (Effective)", "Core #3 (Effective)", "Core #5 (Effective)", "Core #7 (Effective)"]
temp_sensor = "AMD Ryzen 7 7800X3D/Temperatures/Core (Tctl/Tdie)"
temp_base_c = 20
temp_warn_c = 70
tjmax_c = 89
tdp_w = 120
poll_min_ms = 200
idle_poll_s = 5
fluent_ram_s = 1
"""

NO_SENSORS = """
[server]
token = "abc"

[meters]
cpus = [0, 4]
"""


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("FLUENTMON_CONFIG", raising=False)
    monkeypatch.delenv("ProgramData", raising=False)
    return tmp_path


def test_sample_loads_saves_and_loads_equal(isolated: Path) -> None:
    source = isolated / "config.toml"
    source.write_text(SAMPLE, encoding="utf-8")
    first = load_config(source)
    assert first.server.port == 8765
    assert first.server.host == "0.0.0.0"
    assert first.server.safety_read_s == 0.0
    assert first.server.allow_control is False
    assert first.server.ntfy_topic == ""
    assert first.meters.pkg_max_w == 162.0
    assert first.meters.clock_sensors[0] == "Core #1 (Effective)"
    assert first.source == source

    copy = isolated / "copy.toml"
    save_config(first, copy)
    second = load_config(copy)
    assert second == first
    assert second.source == copy


def test_missing_sensor_keys_load_as_none_and_empty(isolated: Path) -> None:
    path = isolated / "config.toml"
    path.write_text(NO_SENSORS, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.meters.power_sensor is None
    assert cfg.meters.temp_sensor is None
    assert cfg.meters.clock_sensors == []
    assert cfg.meters.cpus == [0, 4]
    assert cfg.server.token == "abc"


def test_missing_token_loads_as_empty_string(isolated: Path) -> None:
    path = isolated / "config.toml"
    path.write_text("[server]\nport = 9000\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.server.token == ""
    assert cfg.server.port == 9000


def test_no_file_gives_sample_defaults(isolated: Path) -> None:
    cfg = load_config(isolated / "absent.toml")
    assert cfg.source is None
    assert cfg.server.port == 8765
    assert cfg.meters.power_sensor == "AMD Ryzen 7 7800X3D/Powers/Package"
    assert cfg.meters.clock_sensors == [
        "Core #1 (Effective)",
        "Core #3 (Effective)",
        "Core #5 (Effective)",
        "Core #7 (Effective)",
    ]


def test_save_round_trips_none_sensors(isolated: Path) -> None:
    cfg = Config()
    cfg.meters.power_sensor = None
    cfg.meters.temp_sensor = None
    cfg.meters.clock_sensors = []
    path = isolated / "out" / "config.toml"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.meters.power_sensor is None
    assert loaded.meters.temp_sensor is None
    assert loaded.meters.clock_sensors == []


def test_search_order(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = isolated / "explicit.toml"
    env_file = isolated / "env.toml"
    program_data = isolated / "programdata"
    pd_file = program_data / "FluentMon" / "config.toml"
    cwd_file = Path.cwd() / "config.toml"
    for path in (explicit, env_file, pd_file, cwd_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    monkeypatch.setenv("FLUENTMON_CONFIG", str(env_file))
    monkeypatch.setenv("ProgramData", str(program_data))

    assert find_config(explicit) == explicit
    assert find_config(isolated / "missing.toml") == env_file
    explicit.unlink()
    assert find_config(None) == env_file
    env_file.unlink()
    assert find_config(None) == pd_file
    pd_file.unlink()
    assert find_config(None) == cwd_file
    cwd_file.unlink()
    assert find_config(None) is None


def test_programdata_unset_is_skipped(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ProgramData", raising=False)
    assert find_config(None) is None


def test_save_leaves_no_temp_file(isolated: Path) -> None:
    target_dir = isolated / "cfg"
    save_config(Config(), target_dir / "config.toml")
    assert sorted(p.name for p in target_dir.iterdir()) == ["config.toml"]


def test_unknown_key_is_ignored_with_warning(isolated: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = isolated / "config.toml"
    path.write_text("[server]\nbogus = 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="fluentmon.config"):
        cfg = load_config(path)
    assert cfg.server.port == 8765
    assert any("bogus" in record.getMessage() for record in caplog.records)


def test_wrong_type_names_the_key(isolated: Path) -> None:
    path = isolated / "config.toml"
    path.write_text('[server]\nport = "8765"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"server\.port"):
        load_config(path)


def test_bool_is_not_an_int(isolated: Path) -> None:
    path = isolated / "config.toml"
    path.write_text("[server]\nport = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"server\.port"):
        load_config(path)


def test_target_must_be_positive(isolated: Path) -> None:
    path = isolated / "config.toml"
    path.write_text("[convergence]\ntarget = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_cpus_must_be_non_negative(isolated: Path) -> None:
    path = isolated / "config.toml"
    path.write_text("[meters]\ncpus = [0, -4]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_list_of_wrong_items_is_rejected(isolated: Path) -> None:
    path = isolated / "config.toml"
    path.write_text('[meters]\ncpus = ["a"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"meters\.cpus"):
        load_config(path)
