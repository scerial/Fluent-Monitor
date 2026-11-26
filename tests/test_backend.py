import copy
import io
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from fluentmon.setup.backend import MUTATING, Backend, CpuTopology, FirewallRule
from fluentmon.setup.fake import DryRunBackend, FakeBackend

PROTOCOL_NAMES = sorted(n for n in vars(Backend) if not n.startswith("_"))

ODD_DIR = Path("C:/Users/tester/한글 folder/FluentMon")

MUTATING_CALLS = {
    "relaunch_elevated": (["setup", "--yes"],),
    "fetch": ("https://example.test/lhm.zip", ODD_DIR / "downloads" / "lhm.zip"),
    "write_bytes": (ODD_DIR / "state.json", b"{}"),
    "append_text": (ODD_DIR / "setup.log", "line\n"),
    "mkdir": (ODD_DIR,),
    "delete": (ODD_DIR / "state.json",),
    "rmtree": (ODD_DIR,),
    "copy": (ODD_DIR / "a.toml", ODD_DIR / "b.toml"),
    "unzip": (ODD_DIR / "lhm.zip", ODD_DIR / "lhm"),
    "restrict_acl": (ODD_DIR / "config.toml",),
    "stop_process": ("LibreHardwareMonitor.exe",),
    "task_register": ("FluentMon", ODD_DIR / "pythonw.exe", "-m fluentmon", True, 1),
    "task_unregister": ("FluentMon",),
    "task_start": ("FluentMon",),
    "task_stop": ("FluentMon",),
    "fw_set": (FirewallRule("FluentMon", 8765, "Private", "LocalSubnet", "p.exe"),),
    "fw_remove": ("FluentMon",),
}


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_protocol_names_all_exist_on_fake_and_dry_run() -> None:
    fake = FakeBackend()
    dry = DryRunBackend(fake, io.StringIO())
    assert len(PROTOCOL_NAMES) == 36
    for name in PROTOCOL_NAMES:
        assert callable(getattr(fake, name)), name
        assert callable(getattr(dry, name)), name


def test_mutating_names_are_protocol_names() -> None:
    assert MUTATING <= set(PROTOCOL_NAMES)
    assert set(MUTATING_CALLS) == MUTATING


def test_write_then_read_back_byte_for_byte() -> None:
    fake = FakeBackend()
    path = ODD_DIR / "config.toml"
    payload = "token = \"값 with spaces\"\n".encode("utf-8") + bytes(range(256))
    fake.write_bytes(path, payload)
    assert fake.read_bytes(path) == payload
    names = [name for name, _ in fake.calls]
    assert names == ["write_bytes", "read_bytes"]


def test_read_of_missing_file_is_none() -> None:
    assert FakeBackend().read_bytes(ODD_DIR / "missing.json") is None


def test_fail_on_raises_once_then_works() -> None:
    fake = FakeBackend()
    fake.downloads["https://example.test/lhm.zip"] = b"zip"
    fake.fail_on("fetch", OSError("offline"))
    with pytest.raises(OSError, match="offline"):
        fake.fetch("https://example.test/lhm.zip", ODD_DIR / "lhm.zip")
    fake.fetch("https://example.test/lhm.zip", ODD_DIR / "lhm.zip")
    assert fake.read_bytes(ODD_DIR / "lhm.zip") == b"zip"


def test_fetch_of_unknown_url_names_the_url() -> None:
    with pytest.raises(OSError, match="https://example.test/none.zip"):
        FakeBackend().fetch("https://example.test/none.zip", ODD_DIR / "x.zip")


def test_unzip_extracts_normal_archive() -> None:
    fake = FakeBackend()
    fake.write_bytes(ODD_DIR / "lhm.zip", make_zip({"LibreHardwareMonitor.exe": b"exe", "sub/a.dll": b"dll"}))
    fake.unzip(ODD_DIR / "lhm.zip", ODD_DIR / "lhm")
    assert fake.read_bytes(ODD_DIR / "lhm" / "LibreHardwareMonitor.exe") == b"exe"
    assert fake.read_bytes(ODD_DIR / "lhm" / "sub" / "a.dll") == b"dll"


def test_unzip_rejects_zip_slip() -> None:
    fake = FakeBackend()
    fake.write_bytes(ODD_DIR / "bad.zip", make_zip({"../escape.txt": b"x"}))
    with pytest.raises(OSError):
        fake.unzip(ODD_DIR / "bad.zip", ODD_DIR / "lhm")
    assert fake.exists(ODD_DIR.parent / "escape.txt") is False


def test_rmtree_removes_only_the_subtree() -> None:
    fake = FakeBackend()
    fake.write_bytes(ODD_DIR / "a.txt", b"a")
    fake.write_bytes(ODD_DIR.parent / "keep.txt", b"k")
    fake.rmtree(ODD_DIR)
    assert fake.read_bytes(ODD_DIR / "a.txt") is None
    assert fake.read_bytes(ODD_DIR.parent / "keep.txt") == b"k"


def test_task_lifecycle_on_fake() -> None:
    fake = FakeBackend()
    assert fake.task_last_result("FluentMon") is None
    fake.task_register("FluentMon", ODD_DIR / "pythonw.exe", "-m fluentmon", True, 1)
    assert fake.task_exists("FluentMon")
    assert fake.task_last_result("FluentMon") is None
    fake.task_start("FluentMon")
    assert fake.task_last_result("FluentMon") == 0
    fake.task_unregister("FluentMon")
    assert not fake.task_exists("FluentMon")


def test_dry_run_leaves_state_unchanged_and_prints_one_line_per_call() -> None:
    fake = FakeBackend()
    fake.write_bytes(ODD_DIR / "keep.txt", b"keep")
    fake.task_register("Old", ODD_DIR / "x.exe", "", False, None)
    fake.fw_set(FirewallRule("Old", 1, "Private", "LocalSubnet", "x.exe"))
    fake.downloads["https://example.test/lhm.zip"] = b"zip"
    before = (
        dict(fake.files),
        set(fake.dirs),
        copy.deepcopy(fake.tasks),
        dict(fake.rules),
        list(fake.calls),
    )
    out = io.StringIO()
    dry = DryRunBackend(fake, out)
    for name in sorted(MUTATING):
        result = getattr(dry, name)(*MUTATING_CALLS[name])
        if name == "relaunch_elevated":
            assert result == 0
        else:
            assert result is None
    after = (
        dict(fake.files),
        set(fake.dirs),
        copy.deepcopy(fake.tasks),
        dict(fake.rules),
        list(fake.calls),
    )
    assert after == before
    lines = out.getvalue().splitlines()
    assert len(lines) == len(MUTATING)
    for name in MUTATING:
        assert any(line.startswith(f"would {name}") for line in lines), name


def test_dry_run_forwards_read_methods() -> None:
    fake = FakeBackend(topology=CpuTopology("Intel Core i7", 6, 12), admin=False)
    dry = DryRunBackend(fake, io.StringIO())
    assert dry.cpu_topology() == CpuTopology("Intel Core i7", 6, 12)
    assert dry.is_admin() is False
    assert dry.now() == fake.now_value
    assert isinstance(dry.now(), datetime)
    assert dry.task_exists("nothing") is False
    assert ("cpu_topology", ()) in fake.calls


def test_dry_run_mutating_call_does_not_reach_inner() -> None:
    fake = FakeBackend()
    fake.fail_on("write_bytes", OSError("must not be raised in dry run"))
    dry = DryRunBackend(fake, io.StringIO())
    dry.write_bytes(ODD_DIR / "x", b"x")
    assert fake.calls == []
