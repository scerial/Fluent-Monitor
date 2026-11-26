import io
from pathlib import Path

import orjson
import pytest

from fluentmon.setup.fake import FakeBackend
from fluentmon.setup.manifest import Manifest, ManifestError
from fluentmon.setup.runner import Context, StepError, run_steps, undo_steps

STATE = Path("C:/ProgramData/FluentMon/setup-state.json")
LOG = Path("C:/ProgramData/FluentMon/logs/setup.log")


class RecordingStep:
    def __init__(
        self,
        step_id: str,
        events: list[tuple[str, str]],
        undoable: bool = True,
        done_now: bool = False,
        fail: bool = False,
    ) -> None:
        self.id = step_id
        self.undoable = undoable
        self.done_now = done_now
        self.fail = fail
        self.events = events

    def done(self, ctx: Context) -> bool:
        self.events.append(("done", self.id))
        return self.done_now or ctx.manifest.get(self.id) is not None

    def run(self, ctx: Context) -> dict:
        self.events.append(("run", self.id))
        if self.fail:
            raise StepError(f"{self.id} broke")
        return {"value": self.id, "path": Path("C:/x y/한글")}

    def undo(self, ctx: Context, entry: dict) -> None:
        self.events.append(("undo", self.id))


class BrokenDoneStep(RecordingStep):
    def done(self, ctx: Context) -> bool:
        raise OSError("probe failed")


def make_ctx(backend: FakeBackend) -> Context:
    manifest = Manifest(STATE, backend)
    manifest.load()
    return Context(
        backend=backend,
        manifest=manifest,
        out=io.StringIO(),
        config_path=Path("C:/ProgramData/FluentMon/config.toml"),
        log_path=LOG,
    )


def reload(backend: FakeBackend) -> Manifest:
    manifest = Manifest(STATE, backend)
    manifest.load()
    return manifest


def test_second_run_runs_no_step_again() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    steps = [RecordingStep("a", events), RecordingStep("b", events)]
    assert run_steps(steps, make_ctx(backend)) == 0
    events.clear()
    assert run_steps(steps, make_ctx(backend)) == 0
    assert events == [("done", "a"), ("done", "b")]


def test_failure_at_third_step_then_rerun_continues_from_it() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    steps = [
        RecordingStep("one", events),
        RecordingStep("two", events),
        RecordingStep("three", events, fail=True),
        RecordingStep("four", events),
    ]
    ctx = make_ctx(backend)
    assert run_steps(steps, ctx) == 1
    assert [e["id"] for e in reload(backend).entries()] == ["one", "two"]
    assert "step three failed: three broke" in ctx.out.getvalue()
    steps[2].fail = False
    events.clear()
    assert run_steps(steps, make_ctx(backend)) == 0
    assert [e for e in events if e[0] == "run"] == [("run", "three"), ("run", "four")]
    assert [e["id"] for e in reload(backend).entries()] == ["one", "two", "three", "four"]


def test_undo_runs_in_reverse_and_skips_non_undoable() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    steps = [
        RecordingStep("a", events),
        RecordingStep("b", events, undoable=False),
        RecordingStep("c", events),
    ]
    run_steps(steps, make_ctx(backend))
    events.clear()
    assert undo_steps(steps, make_ctx(backend)) == 0
    assert events == [("undo", "c"), ("undo", "a")]
    assert [e["id"] for e in reload(backend).entries()] == ["b"]


def test_undo_removes_only_undone_entries() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    steps = [RecordingStep("a", events), RecordingStep("b", events, undoable=False)]
    run_steps(steps, make_ctx(backend))
    undo_steps(steps, make_ctx(backend))
    assert [e["id"] for e in reload(backend).entries()] == ["b"]


def test_failing_undo_is_reported_and_others_still_run() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    steps = [RecordingStep("a", events), RecordingStep("b", events)]
    run_steps(steps, make_ctx(backend))

    class FailingUndo(RecordingStep):
        def undo(self, ctx: Context, entry: dict) -> None:
            self.events.append(("undo", self.id))
            raise OSError("cannot undo b")

    steps = [RecordingStep("a", events), FailingUndo("b", events)]
    ctx = make_ctx(backend)
    assert undo_steps(steps, ctx) == 1
    assert ("undo", "a") in events
    assert "undo of step b failed: cannot undo b" in ctx.out.getvalue()
    assert [e["id"] for e in reload(backend).entries()] == ["b"]


def test_manifest_survives_reload_with_equal_content() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    run_steps([RecordingStep("a", events), RecordingStep("b", events)], make_ctx(backend))
    first = reload(backend).entries()
    second = reload(backend).entries()
    assert first == second
    assert first[0]["value"] == "a"
    assert first[0]["done"] is True
    assert first[0]["at"] == backend.now_value.isoformat()


def test_path_fields_are_stored_as_strings() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    run_steps([RecordingStep("a", events)], make_ctx(backend))
    assert reload(backend).get("a")["path"] == "C:/x y/한글"


def test_record_twice_replaces_in_place_and_keeps_position() -> None:
    backend = FakeBackend()
    manifest = reload(backend)
    manifest.record("a", value=1)
    manifest.record("b", value=2)
    manifest.record("a", value=3)
    entries = reload(backend).entries()
    assert [e["id"] for e in entries] == ["a", "b"]
    assert entries[0]["value"] == 3


def test_wrong_schema_raises_manifest_error() -> None:
    backend = FakeBackend()
    backend.write_bytes(STATE, orjson.dumps({"schema": 2, "steps": []}))
    with pytest.raises(ManifestError):
        reload(backend)


def test_unparseable_file_raises_manifest_error() -> None:
    backend = FakeBackend()
    backend.write_bytes(STATE, b"{not json")
    with pytest.raises(ManifestError):
        reload(backend)


def test_missing_file_gives_empty_manifest() -> None:
    assert reload(FakeBackend()).entries() == []


def test_interrupted_run_leaves_earlier_entries_only() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    steps = [RecordingStep("a", events), RecordingStep("b", events, fail=True)]
    run_steps(steps, make_ctx(backend))
    manifest = reload(backend)
    assert manifest.get("a") is not None
    assert manifest.get("b") is None


def test_done_that_raises_is_treated_as_not_done_and_logged() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    ctx = make_ctx(backend)
    assert run_steps([BrokenDoneStep("a", events)], ctx) == 0
    assert ("run", "a") in events
    log_text = backend.read_bytes(LOG).decode("utf-8")
    assert "a check failed, treated as not done: probe failed" in log_text


def test_log_line_format() -> None:
    backend = FakeBackend()
    events: list[tuple[str, str]] = []
    run_steps([RecordingStep("a", events)], make_ctx(backend))
    lines = backend.read_bytes(LOG).decode("utf-8").splitlines()
    assert lines[-1] == f"{backend.now_value.isoformat()} a run done"
