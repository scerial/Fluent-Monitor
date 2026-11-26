import io
from pathlib import Path

import pytest

from fluentmon.setup.backend import CpuTopology
from fluentmon.setup.confirm import DetectStep, confirm
from fluentmon.setup.detect import Detected, detect
from fluentmon.setup.fake import DryRunBackend, FakeBackend
from fluentmon.setup.manifest import Manifest
from fluentmon.setup.pins import LHM_DATA_URL
from fluentmon.setup.runner import Context, StepError, run_steps
from support import make_machine, make_tree, hardware_node

AMD_NAME = "AMD Ryzen 7 7800X3D"
AMD_TOPOLOGY = CpuTopology(AMD_NAME, 8, 16)
AMD_SENSORS = {
    "Powers": {"Package": "42.1 W", "Cores": "30 W"},
    "Clocks": {
        "Core #1 (Effective)": "4500 MHz",
        "Core #3 (Effective)": "4450 MHz",
        "Core #5 (Effective)": "4400 MHz",
        "Core #7 (Effective)": "4600 MHz",
    },
    "Temperatures": {
        "Core (Tctl/Tdie)": "55.0 °C",
        "Core Max": "70 °C",
        "CCD1 (Tdie)": "61 °C",
    },
}
SECTION_5_SCREEN = (
    "1  cpus           [0, 4, 8, 12]\n"
    f"2  power_sensor   {AMD_NAME}/Powers/Package\n"
    "3  clock_sensors  Core #1 (Effective), Core #3 (Effective), "
    "Core #5 (Effective), Core #7 (Effective)\n"
    f"4  temp_sensor    {AMD_NAME}/Temperatures/Core (Tctl/Tdie)\n"
    "5  tdp_w          120\n"
    "6  pkg_max_w      162\n"
    "7  tjmax_c        89\n"
    "8  watch_dir      C:/FluentMon/transcripts\n"
    "9  runs_dir       C:/FluentMon/runs\n"
    "[Enter] accept all   [1-9] change a value   [q] quit without changes\n"
)


def scripted(answers: list[str]):
    items = iter(answers)

    def ask(prompt: str) -> str:
        try:
            return next(items)
        except StopIteration:
            raise EOFError

    return ask


def amd_detected() -> Detected:
    return detect(make_tree(AMD_NAME, AMD_SENSORS), AMD_TOPOLOGY)


def test_screen_text_for_section_5_values() -> None:
    out = io.StringIO()
    confirm(amd_detected(), 16, scripted([""]), out, False)
    assert out.getvalue() == SECTION_5_SCREEN


def test_enter_alone_returns_detected_values() -> None:
    d = amd_detected()
    assert confirm(d, 16, scripted([""]), io.StringIO(), False) == d


def test_row_4_then_first_alternative_sets_temperature() -> None:
    result = confirm(amd_detected(), 16, scripted(["4", "1", ""]), io.StringIO(), False)
    assert result.temp_sensor == f"{AMD_NAME}/Temperatures/Core Max"


def test_row_7_typed_value_sets_tjmax() -> None:
    result = confirm(amd_detected(), 16, scripted(["7", "0", "95", ""]), io.StringIO(), False)
    assert result.tjmax_c == 95.0


def test_invalid_row_number_is_reported_and_next_answer_used() -> None:
    out = io.StringIO()
    d = amd_detected()
    assert confirm(d, 16, scripted(["42", ""]), out, False) == d
    assert "choose a row from 1 to 9" in out.getvalue()


def test_invalid_typed_cpu_is_asked_again() -> None:
    out = io.StringIO()
    result = confirm(amd_detected(), 16, scripted(["1", "0", "99", "0,2", ""]), out, False)
    assert result.cpus == [0, 2]
    assert "invalid value" in out.getvalue()


def test_clock_sensors_cleared_with_empty_entry() -> None:
    result = confirm(amd_detected(), 16, scripted(["3", "0", ""]), io.StringIO(), False)
    assert result.clock_sensors == []


def test_unknown_cpu_asks_tdp_and_tjmax_then_derives_pkg_max() -> None:
    name = "Mystery Chip 9000"
    d = detect(make_tree(name, {"Powers": {"Package": "42 W"}}), CpuTopology(name, 8, 16))
    answers = iter(["130", "95", ""])
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    result = confirm(d, 16, ask, io.StringIO(), False)
    assert result.tdp_w == 130.0
    assert result.tjmax_c == 95.0
    assert result.pkg_max_w == 176.0
    assert prompts[:2] == ["TDP in watts:", "Tjmax in degrees C:"]


def test_yes_returns_input_unchanged_with_screen_only() -> None:
    d = amd_detected()
    out = io.StringIO()
    assert confirm(d, 16, scripted(["5"]), out, True) == d
    assert out.getvalue() == SECTION_5_SCREEN


def test_yes_with_unknown_cpu_raises() -> None:
    name = "Mystery Chip 9000"
    d = detect(make_tree(name, {"Powers": {"Package": "42 W"}}), CpuTopology(name, 8, 16))
    with pytest.raises(StepError, match="tdp_w and tjmax_c are unknown for this CPU; run setup without --yes"):
        confirm(d, 16, scripted([]), io.StringIO(), True)


def test_eof_accepts_all() -> None:
    d = amd_detected()
    assert confirm(d, 16, scripted([]), io.StringIO(), False) == d


def test_q_stops_with_error() -> None:
    with pytest.raises(StepError, match="^setup cancelled$"):
        confirm(amd_detected(), 16, scripted(["q"]), io.StringIO(), False)


def test_q_in_detect_step_keeps_earlier_manifest_entries() -> None:
    from fluentmon.setup.runner import run_steps
    from fluentmon.setup.manifest import Manifest

    backend = FakeBackend()
    backend.json_by_url[LHM_DATA_URL] = make_tree(AMD_NAME, AMD_SENSORS)
    ctx = make_ctx(backend, io.StringIO())
    ctx.manifest.record("earlier", value=1)
    assert run_steps([DetectStep(scripted(["q"]), sleep=lambda _: None)], ctx) == 1
    assert "setup cancelled" in ctx.out.getvalue()
    reloaded = Manifest(Path("C:/ProgramData/FluentMon/setup-state.json"), backend)
    reloaded.load()
    assert [e["id"] for e in reloaded.entries()] == ["earlier"]


def test_answer_with_spaces_is_accepted() -> None:
    result = confirm(amd_detected(), 16, scripted(["  4  ", "  1  ", "  "]), io.StringIO(), False)
    assert result.temp_sensor == f"{AMD_NAME}/Temperatures/Core Max"


class SlowLhm(FakeBackend):
    answer_on: int = 3

    def http_get_json(self, url: str, timeout_s: float = 1.0) -> dict | None:
        self._call("http_get_json", url, timeout_s)
        polls = sum(1 for name, _ in self.calls if name == "http_get_json")
        if polls < self.answer_on:
            return None
        return dict(self.json_by_url[url])


def make_ctx(backend: FakeBackend, out: io.StringIO, dry_run: bool = False, yes: bool = False) -> Context:
    manifest = Manifest(Path("C:/ProgramData/FluentMon/setup-state.json"), backend)
    manifest.load()
    return Context(
        backend=backend,
        manifest=manifest,
        out=out,
        config_path=Path("C:/ProgramData/FluentMon/config.toml"),
        log_path=Path("C:/ProgramData/FluentMon/logs/setup.log"),
        dry_run=dry_run,
        yes=yes,
    )


def test_detect_step_succeeds_on_third_poll() -> None:
    backend = SlowLhm()
    backend.json_by_url[LHM_DATA_URL] = make_tree(AMD_NAME, AMD_SENSORS)
    sleeps: list[float] = []
    out = io.StringIO()
    step = DetectStep(scripted([""]), sleep=sleeps.append)
    fields = step.run(make_ctx(backend, out))
    assert sleeps == [1.0, 1.0]
    assert fields["cpu_name"] == AMD_NAME
    assert fields["values"]["power_sensor"] == f"{AMD_NAME}/Powers/Package"
    assert fields["values"]["cpus"] == [0, 4, 8, 12]
    assert fields["values"]["watch_dir"] == "C:/FluentMon/transcripts"
    assert "CPU            AMD Ryzen 7 7800X3D, 8 cores, 16 threads" in out.getvalue()


def test_detect_step_no_answer_after_twenty_polls_raises() -> None:
    backend = FakeBackend()
    sleeps: list[float] = []
    step = DetectStep(scripted([""]), sleep=sleeps.append)
    with pytest.raises(StepError, match="LHM data.json is not reachable on 127.0.0.1:8085"):
        step.run(make_ctx(backend, io.StringIO()))
    assert len(sleeps) == 19


def test_detect_step_no_cpu_sensors_raises_with_driver_hint() -> None:
    backend = FakeBackend()
    backend.json_by_url[LHM_DATA_URL] = make_tree("Motherboard", {"Temperatures": {"System": "40 °C"}})
    step = DetectStep(scripted([""]), sleep=lambda _: None)
    with pytest.raises(StepError, match="security software may be blocking its driver"):
        step.run(make_ctx(backend, io.StringIO()))


def test_detect_step_dry_run_prints_and_returns_empty_values() -> None:
    fake = FakeBackend()
    out = io.StringIO()
    ctx = make_ctx(fake, out, dry_run=True)
    ctx.backend = DryRunBackend(fake, out)
    step = DetectStep(scripted([]), sleep=lambda _: None)
    fields = step.run(ctx)
    assert fields == {"cpu_name": AMD_NAME, "values": {}}
    assert "would detect sensors" in out.getvalue()
    assert not any(name == "http_get_json" for name, _ in fake.calls)


def test_detect_step_is_skipped_when_done() -> None:
    backend = FakeBackend()
    backend.json_by_url[LHM_DATA_URL] = make_tree(AMD_NAME, AMD_SENSORS)
    step = DetectStep(scripted([""]), sleep=lambda _: None)
    run_steps([step], make_ctx(backend, io.StringIO()))
    polls_after_first = sum(1 for name, _ in backend.calls if name == "http_get_json")
    run_steps([step], make_ctx(backend, io.StringIO()))
    assert sum(1 for name, _ in backend.calls if name == "http_get_json") == polls_after_first


def test_detect_step_with_yes_and_unknown_cpu_raises() -> None:
    backend = FakeBackend(topology=CpuTopology("Mystery Chip 9000", 8, 16))
    backend.json_by_url[LHM_DATA_URL] = make_machine(
        [hardware_node("Mystery Chip 9000", {"Powers": {"Package": "42 W"}, "Clocks": {"Core #1": "1 MHz"}})]
    )
    step = DetectStep(scripted([]), sleep=lambda _: None)
    ctx = make_ctx(backend, io.StringIO(), yes=True)
    with pytest.raises(StepError, match="tdp_w and tjmax_c are unknown"):
        step.run(ctx)
