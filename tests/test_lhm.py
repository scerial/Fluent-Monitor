import hashlib
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from fluentmon.setup import pins
from fluentmon.setup.backend import MUTATING
from fluentmon.setup.fake import DryRunBackend, FakeBackend
from fluentmon.setup.lhm import WEB_SERVER_KEYS, LhmSettingsStep, LhmStep, apply_settings
from fluentmon.setup.manifest import Manifest
from fluentmon.setup.runner import Context, StepError, run_steps, undo_steps

CONFIG = Path("C:/ProgramData/FluentMon/config.toml")
LOG = Path("C:/ProgramData/FluentMon/logs/setup.log")
ARCHIVE = Path("C:/ProgramData/FluentMon/downloads/lhm.zip")
INSTALL_PREFIX = "C:/Program Files/FluentMon/LibreHardwareMonitor"


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_ctx(backend, out=None, dry_run=False, lhm_zip=None) -> Context:
    manifest = Manifest(Path("C:/ProgramData/FluentMon/setup-state.json"), backend)
    manifest.load()
    return Context(
        backend=backend,
        manifest=manifest,
        out=out or io.StringIO(),
        config_path=CONFIG,
        log_path=LOG,
        dry_run=dry_run,
        lhm_zip=lhm_zip,
    )


@pytest.fixture
def lhm_zip_bytes() -> bytes:
    return make_zip({"LibreHardwareMonitor.exe": b"exe", "LibreHardwareMonitor.dll": b"dll"})


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, lhm_zip_bytes: bytes) -> bytes:
    monkeypatch.setattr(pins, "LHM_URL", "https://example.test/lhm.zip")
    monkeypatch.setattr(pins, "LHM_SHA256", sha(lhm_zip_bytes))
    return lhm_zip_bytes


def fetch_count(backend: FakeBackend) -> int:
    return sum(1 for name, _ in backend.calls if name == "fetch")


def test_download_unpack_and_manifest_fields(pinned: bytes) -> None:
    backend = FakeBackend()
    backend.downloads[pins.LHM_URL] = pinned
    ctx = make_ctx(backend)
    assert run_steps([LhmStep()], ctx) == 0
    assert backend.exists(pins.INSTALL_DIR / pins.LHM_EXE)
    entry = ctx.manifest.get("lhm")
    assert entry["folder"] == str(pins.INSTALL_DIR)
    assert entry["sha256"] == sha(pinned)
    assert entry["version"] == pins.LHM_VERSION
    assert entry["installed"] is True


def test_rerun_skips_download(pinned: bytes) -> None:
    backend = FakeBackend()
    backend.downloads[pins.LHM_URL] = pinned
    run_steps([LhmStep()], make_ctx(backend))
    run_steps([LhmStep()], make_ctx(backend))
    assert fetch_count(backend) == 1


def test_wrong_hash_raises_and_removes_downloaded_zip(pinned: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pins, "LHM_SHA256", "0" * 64)
    backend = FakeBackend()
    backend.downloads[pins.LHM_URL] = pinned
    with pytest.raises(StepError, match="expected 0{64}, got"):
        LhmStep().run(make_ctx(backend))
    assert backend.read_bytes(ARCHIVE) is None


def test_wrong_hash_keeps_user_supplied_zip(pinned: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pins, "LHM_SHA256", "0" * 64)
    backend = FakeBackend()
    user_zip = Path("C:/Users/tester/Downloads/lhm.zip")
    backend.write_bytes(user_zip, pinned)
    with pytest.raises(StepError, match="hash mismatch"):
        LhmStep().run(make_ctx(backend, lhm_zip=user_zip))
    assert backend.read_bytes(user_zip) == pinned


def test_lhm_zip_option_unpacks_without_download(pinned: bytes) -> None:
    backend = FakeBackend()
    user_zip = Path("C:/Users/tester/Downloads/lhm.zip")
    backend.write_bytes(user_zip, pinned)
    assert run_steps([LhmStep()], make_ctx(backend, lhm_zip=user_zip)) == 0
    assert backend.exists(pins.INSTALL_DIR / pins.LHM_EXE)
    assert fetch_count(backend) == 0


def test_corrupt_zip_unpacks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    junk = b"this is not a zip"
    monkeypatch.setattr(pins, "LHM_SHA256", sha(junk))
    backend = FakeBackend()
    user_zip = Path("C:/Users/tester/Downloads/bad.zip")
    backend.write_bytes(user_zip, junk)
    with pytest.raises(StepError, match="cannot unpack"):
        LhmStep().run(make_ctx(backend, lhm_zip=user_zip))
    assert not any(key.startswith(INSTALL_PREFIX) for key in backend.files)


def test_download_failure_names_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pins, "LHM_URL", "https://example.test/lhm.zip")
    with pytest.raises(StepError, match="https://example.test/lhm.zip"):
        LhmStep().run(make_ctx(FakeBackend()))


def test_zip_with_extra_folder_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_zip({"LHM-0.9/LibreHardwareMonitor.exe": b"exe"})
    monkeypatch.setattr(pins, "LHM_SHA256", sha(data))
    backend = FakeBackend()
    user_zip = Path("C:/Users/tester/Downloads/lhm.zip")
    backend.write_bytes(user_zip, data)
    with pytest.raises(StepError, match="not directly inside"):
        LhmStep().run(make_ctx(backend, lhm_zip=user_zip))


def test_apply_settings_on_none_has_both_keys() -> None:
    root = ET.fromstring(apply_settings(None, WEB_SERVER_KEYS))
    values = {e.get("key"): e.get("value") for e in root.find("appSettings").findall("add")}
    assert values == WEB_SERVER_KEYS


def test_apply_settings_keeps_unrelated_key() -> None:
    original = b'<?xml version="1.0"?><configuration><appSettings><add key="Other" value="x"/></appSettings></configuration>'
    root = ET.fromstring(apply_settings(original, WEB_SERVER_KEYS))
    values = {e.get("key"): e.get("value") for e in root.find("appSettings").findall("add")}
    assert values["Other"] == "x"
    assert values["listenerPort"] == "8085"


def test_apply_settings_replaces_wrong_port() -> None:
    original = b'<configuration><appSettings><add key="listenerPort" value="8080"/></appSettings></configuration>'
    root = ET.fromstring(apply_settings(original, WEB_SERVER_KEYS))
    ports = [e.get("value") for e in root.find("appSettings").findall("add") if e.get("key") == "listenerPort"]
    assert ports == ["8085"]


def test_settings_step_backs_up_and_undo_restores_bytes() -> None:
    backend = FakeBackend()
    original = b'<configuration><appSettings><add key="listenerPort" value="8080"/></appSettings></configuration>'
    backend.write_bytes(pins.LHM_SETTINGS, original)
    ctx = make_ctx(backend)
    assert run_steps([LhmSettingsStep()], ctx) == 0
    backup = pins.LHM_SETTINGS.with_name(pins.LHM_SETTINGS.name + ".fmon-backup")
    assert backend.read_bytes(backup) == original
    assert b'value="8085"' in backend.read_bytes(pins.LHM_SETTINGS)
    undo_steps([LhmSettingsStep()], ctx)
    assert backend.read_bytes(pins.LHM_SETTINGS) == original
    assert backend.read_bytes(backup) is None


def test_settings_step_without_file_undo_deletes_it() -> None:
    backend = FakeBackend()
    ctx = make_ctx(backend)
    run_steps([LhmSettingsStep()], ctx)
    assert backend.read_bytes(pins.LHM_SETTINGS) is not None
    assert ctx.manifest.get("lhm_settings")["backup"] is None
    undo_steps([LhmSettingsStep()], ctx)
    assert backend.read_bytes(pins.LHM_SETTINGS) is None


def test_settings_rerun_is_skipped() -> None:
    backend = FakeBackend()
    run_steps([LhmSettingsStep()], make_ctx(backend))
    writes = sum(1 for name, _ in backend.calls if name == "write_bytes")
    run_steps([LhmSettingsStep()], make_ctx(backend))
    assert sum(1 for name, _ in backend.calls if name == "write_bytes") == writes


def test_undo_keeps_a_preexisting_lhm_folder(pinned: bytes) -> None:
    backend = FakeBackend()
    backend.mkdir(pins.INSTALL_DIR)
    backend.write_bytes(pins.INSTALL_DIR / pins.LHM_EXE, b"users own install")
    backend.downloads[pins.LHM_URL] = pinned
    ctx = make_ctx(backend)
    run_steps([LhmStep()], ctx)
    assert ctx.manifest.get("lhm")["installed"] is False
    backend.calls.clear()
    undo_steps([LhmStep()], ctx)
    names = [name for name, _ in backend.calls]
    assert "rmtree" not in names
    assert "stop_process" not in names
    assert ctx.manifest.get("lhm") is None


def test_lhm_undo_stops_process_before_rmtree(pinned: bytes) -> None:
    backend = FakeBackend()
    backend.downloads[pins.LHM_URL] = pinned
    ctx = make_ctx(backend)
    run_steps([LhmStep()], ctx)
    backend.calls.clear()
    undo_steps([LhmStep()], ctx)
    names = [name for name, _ in backend.calls]
    assert names.index("stop_process") < names.index("rmtree")
    assert not backend.exists(pins.INSTALL_DIR / pins.LHM_EXE)


def test_dry_run_skips_read_back_and_changes_nothing(pinned: bytes) -> None:
    fake = FakeBackend()
    fake.downloads[pins.LHM_URL] = pinned
    out = io.StringIO()
    dry = DryRunBackend(fake, out)
    before = (dict(fake.files), set(fake.dirs))
    fields = LhmStep().run(make_ctx(dry, out=out, dry_run=True))
    assert fields["folder"] == str(pins.INSTALL_DIR)
    assert "would fetch" in out.getvalue()
    assert "would unzip" in out.getvalue()
    settings_fields = LhmSettingsStep().run(make_ctx(dry, out=out, dry_run=True))
    assert settings_fields["backup"] is None
    assert "would write_bytes" in out.getvalue()
    assert before == (dict(fake.files), set(fake.dirs))
    assert not any(name in MUTATING for name, _ in fake.calls)
