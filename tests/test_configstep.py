import io
import tomllib
from pathlib import Path

import pytest

from fluentmon.config import Config, MetersCfg, ServerCfg, load_config
from fluentmon.setup.configstep import ConfigStep, build_config, config_text
from fluentmon.setup.fake import FakeBackend
from fluentmon.setup.manifest import Manifest
from fluentmon.setup.runner import Context, StepError, run_steps, undo_steps

CONFIG = Path("C:/ProgramData/FluentMon/config.toml")
LOG = Path("C:/ProgramData/FluentMon/logs/setup.log")
AMD = "AMD Ryzen 7 7800X3D"
SECTION_5 = {
    "cpus": [0, 4, 8, 12],
    "power_sensor": f"{AMD}/Powers/Package",
    "clock_sensors": [
        "Core #1 (Effective)",
        "Core #3 (Effective)",
        "Core #5 (Effective)",
        "Core #7 (Effective)",
    ],
    "temp_sensor": f"{AMD}/Temperatures/Core (Tctl/Tdie)",
    "tdp_w": 120.0,
    "tjmax_c": 89.0,
    "pkg_max_w": 162.0,
    "watch_dir": "C:/FluentMon/transcripts",
    "runs_dir": "C:/FluentMon/runs",
}
NO_SENSORS = {"cpus": [0, 1, 2, 3], "tdp_w": 65.0, "tjmax_c": 100.0, "pkg_max_w": 88.0}
TOKEN_A = "a" * 32
TOKEN_B = "b" * 32


def make_ctx(backend: FakeBackend, values: dict | None = None, rotate: bool = False, purge: bool = False) -> Context:
    manifest = Manifest(Path("C:/ProgramData/FluentMon/setup-state.json"), backend)
    manifest.load()
    if values is not None:
        manifest.record("detect", cpu_name=AMD, values=values)
    return Context(
        backend=backend,
        manifest=manifest,
        out=io.StringIO(),
        config_path=CONFIG,
        log_path=LOG,
        rotate_token=rotate,
        purge=purge,
    )


def counter_tokens():
    values = iter([TOKEN_A, TOKEN_B, "c" * 32])

    def new_token() -> str:
        return next(values)

    return new_token


def test_config_text_round_trips_through_load_config(tmp_path: Path) -> None:
    cfg = build_config(SECTION_5, TOKEN_A, None)
    target = tmp_path / "config.toml"
    target.write_text(config_text(cfg), encoding="utf-8")
    assert load_config(target) == cfg
    assert load_config(target).server.token == TOKEN_A


def test_build_config_routes_folders_to_server_and_sensors_to_meters() -> None:
    cfg = build_config(SECTION_5, TOKEN_A, None)
    assert cfg.server.watch_dir == "C:/FluentMon/transcripts"
    assert cfg.server.runs_dir == "C:/FluentMon/runs"
    assert cfg.meters.power_sensor == f"{AMD}/Powers/Package"
    assert cfg.meters.tdp_w == 120.0


def test_absent_sensor_keys_are_none_or_empty_in_file(tmp_path: Path) -> None:
    cfg = build_config(NO_SENSORS, TOKEN_A, None)
    text = config_text(cfg)
    assert "power_sensor" not in text
    assert "temp_sensor" not in text
    assert "clock_sensors = []" in text
    target = tmp_path / "config.toml"
    target.write_text(text, encoding="utf-8")
    loaded = load_config(target)
    assert loaded.meters.power_sensor is None
    assert loaded.meters.temp_sensor is None
    assert loaded.meters.clock_sensors == []


def test_config_text_has_only_the_three_tables() -> None:
    document = tomllib.loads(config_text(build_config(SECTION_5, TOKEN_A, None)))
    assert set(document) == {"server", "convergence", "meters"}


def test_existing_meters_keys_kept_and_absent_sensor_keys_dropped() -> None:
    backend = FakeBackend()
    original = (
        b'[server]\ntoken = "' + TOKEN_B.encode() + b'"\n'
        b'[meters]\nlhm_url = "http://127.0.0.1:9000"\npower_sensor = "old/Powers/Package"\n'
    )
    backend.write_bytes(CONFIG, original)
    values = {"cpus": [0, 4, 8, 12], "tdp_w": 120.0, "tjmax_c": 89.0, "pkg_max_w": 162.0,
              "watch_dir": "C:/FluentMon/transcripts", "runs_dir": "C:/FluentMon/runs"}
    ConfigStep(counter_tokens()).run(make_ctx(backend, values))
    loaded = tomllib.loads(backend.read_bytes(CONFIG).decode("utf-8"))
    assert loaded["meters"]["lhm_url"] == "http://127.0.0.1:9000"
    assert "power_sensor" not in loaded["meters"]
    assert loaded["meters"]["tdp_w"] == 120.0


def test_forced_rerun_keeps_server_and_convergence_values() -> None:
    backend = FakeBackend()
    original = (
        b'[server]\ntoken = "' + TOKEN_B.encode() + b'"\nport = 9100\nallow_control = true\n'
        b'watch_dir = "D:/old"\n'
        b'[convergence]\ntarget = 0.001\n'
        b'[meters]\nlhm_url = "http://127.0.0.1:9000"\n'
    )
    backend.write_bytes(CONFIG, original)
    values = {"cpus": [0, 4, 8, 12], "tdp_w": 120.0, "tjmax_c": 89.0, "pkg_max_w": 162.0,
              "watch_dir": "C:/FluentMon/transcripts", "runs_dir": "C:/FluentMon/runs"}
    ConfigStep(counter_tokens()).run(make_ctx(backend, values, rotate=True))
    loaded = tomllib.loads(backend.read_bytes(CONFIG).decode("utf-8"))
    assert loaded["server"]["port"] == 9100
    assert loaded["server"]["allow_control"] is True
    assert loaded["server"]["watch_dir"] == "C:/FluentMon/transcripts"
    assert loaded["server"]["runs_dir"] == "C:/FluentMon/runs"
    assert loaded["server"]["token"] == TOKEN_A
    assert loaded["convergence"]["target"] == 0.001
    assert loaded["meters"]["lhm_url"] == "http://127.0.0.1:9000"


def test_build_config_keeps_existing_server_and_convergence_except_token_and_folders() -> None:
    existing = Config()
    existing.server.port = 9100
    existing.server.allow_control = True
    existing.server.watch_dir = "D:/old"
    existing.convergence.target = 0.001
    cfg = build_config(SECTION_5, TOKEN_A, existing)
    assert cfg.server.port == 9100
    assert cfg.server.allow_control is True
    assert cfg.server.token == TOKEN_A
    assert cfg.server.watch_dir == "C:/FluentMon/transcripts"
    assert cfg.convergence.target == 0.001
    assert existing.server.token == ""


def test_build_config_keeps_existing_meters_key_absent_from_values() -> None:
    existing = Config()
    existing.meters.lhm_url = "http://127.0.0.1:9000"
    cfg = build_config({"cpus": [0]}, TOKEN_A, existing)
    assert cfg.meters.lhm_url == "http://127.0.0.1:9000"


def test_unknown_detected_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown detected key: bogus"):
        build_config({"bogus": 1}, TOKEN_A, None)


def test_first_run_is_created_and_folders_exist() -> None:
    backend = FakeBackend()
    ctx = make_ctx(backend, SECTION_5)
    fields = ConfigStep(counter_tokens()).run(ctx)
    assert fields == {"path": str(CONFIG), "mode": "created", "backup": None}
    assert backend.exists(Path("C:/FluentMon/transcripts"))
    assert backend.exists(Path("C:/FluentMon/runs"))
    assert ("restrict_acl", (CONFIG,)) in backend.calls


def test_second_run_keeps_token() -> None:
    backend = FakeBackend()
    step = ConfigStep(counter_tokens())
    step.run(make_ctx(backend, SECTION_5))
    token_first = tomllib.loads(backend.read_bytes(CONFIG).decode())["server"]["token"]
    step.run(make_ctx(backend, SECTION_5))
    token_second = tomllib.loads(backend.read_bytes(CONFIG).decode())["server"]["token"]
    assert token_first == TOKEN_A
    assert token_second == TOKEN_A


def test_rotate_token_changes_token_and_backs_up() -> None:
    backend = FakeBackend()
    step = ConfigStep(counter_tokens())
    step.run(make_ctx(backend, SECTION_5))
    earlier = backend.read_bytes(CONFIG)
    fields = step.run(make_ctx(backend, SECTION_5, rotate=True))
    assert fields["mode"] == "updated"
    assert fields["backup"] is not None
    assert backend.read_bytes(Path(fields["backup"])) == earlier
    assert tomllib.loads(backend.read_bytes(CONFIG).decode())["server"]["token"] == TOKEN_B


def test_token_in_file_is_kept_byte_for_byte_text() -> None:
    backend = FakeBackend()
    step = ConfigStep(counter_tokens())
    step.run(make_ctx(backend, SECTION_5))
    first = backend.read_bytes(CONFIG)
    step.run(make_ctx(backend, SECTION_5))
    assert backend.read_bytes(CONFIG) == first


def test_short_token_in_file_is_replaced() -> None:
    backend = FakeBackend()
    backend.write_bytes(CONFIG, b'[server]\ntoken = "short"\n')
    fields = ConfigStep(counter_tokens()).run(make_ctx(backend, SECTION_5))
    assert fields["mode"] == "updated"
    assert tomllib.loads(backend.read_bytes(CONFIG).decode())["server"]["token"] == TOKEN_A


def test_extra_keys_dropped_and_visible_in_backup() -> None:
    backend = FakeBackend()
    original = b'[server]\ntoken = "' + TOKEN_B.encode() + b'"\nlegacy_key = 1\n'
    backend.write_bytes(CONFIG, original)
    fields = ConfigStep(counter_tokens()).run(make_ctx(backend, SECTION_5))
    assert backend.read_bytes(Path(fields["backup"])) == original
    assert b"legacy_key" not in backend.read_bytes(CONFIG)


def test_unparseable_file_is_backed_up_and_logged() -> None:
    backend = FakeBackend()
    backend.write_bytes(CONFIG, b"[server\nthis is not toml")
    ctx = make_ctx(backend, SECTION_5)
    fields = ConfigStep(counter_tokens()).run(ctx)
    assert fields["backup"] is not None
    assert "could not be parsed; backed up and replaced" in backend.read_bytes(LOG).decode()
    assert tomllib.loads(backend.read_bytes(CONFIG).decode())["server"]["token"] == TOKEN_A


def test_watch_dir_already_present_is_fine() -> None:
    backend = FakeBackend()
    backend.mkdir(Path("C:/FluentMon/transcripts"))
    fields = ConfigStep(counter_tokens()).run(make_ctx(backend, SECTION_5))
    assert fields["mode"] == "created"


def test_missing_detect_entry_raises() -> None:
    backend = FakeBackend()
    with pytest.raises(StepError, match="detect has not run"):
        ConfigStep(counter_tokens()).run(make_ctx(backend))


def test_done_requires_file_and_no_rotation() -> None:
    backend = FakeBackend()
    ctx = make_ctx(backend, SECTION_5)
    run_steps([ConfigStep(counter_tokens())], ctx)
    assert ConfigStep().done(make_ctx(backend, SECTION_5)) is True
    assert ConfigStep().done(make_ctx(backend, SECTION_5, rotate=True)) is False


def test_undo_without_purge_deletes_nothing() -> None:
    backend = FakeBackend()
    ctx = make_ctx(backend, SECTION_5)
    run_steps([ConfigStep(counter_tokens())], ctx)
    undo_steps([ConfigStep()], make_ctx(backend, SECTION_5))
    assert backend.read_bytes(CONFIG) is not None


def test_undo_with_purge_deletes_the_config_file() -> None:
    backend = FakeBackend()
    ctx = make_ctx(backend, SECTION_5)
    run_steps([ConfigStep(counter_tokens())], ctx)
    undo_steps([ConfigStep()], make_ctx(backend, SECTION_5, purge=True))
    assert backend.read_bytes(CONFIG) is None
