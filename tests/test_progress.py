from __future__ import annotations

import math

import pytest

from fluentmon.model.progress import ConvTracker, StepTracker, fmt_hms, iter_bar


def test_fmt_hms() -> None:
    assert fmt_hms(760) == "0:12:40"
    assert fmt_hms(3661) == "1:01:01"
    assert fmt_hms(0) == "0:00:00"


def test_iter_bar_example() -> None:
    bar = iter_bar(1, 484, 516, None, False, False)
    assert bar["p"] == pytest.approx(0.484)
    assert bar["done"] == 484
    assert bar["total"] == 1000
    assert bar["left"] == 516
    assert bar["label"] == "Iteration 484 / 1000 · 516 left"


def test_iter_bar_with_eta() -> None:
    bar = iter_bar(1, 482, 518, 760, False, False)
    assert bar["label"] == "Iteration 482 / 1000 · 518 left · ETA 0:12:40"
    assert bar["eta_s"] == 760


def test_iter_bar_per_step_and_converged() -> None:
    per_step = iter_bar(1, 7, 13, None, False, True)
    assert per_step["label"] == "Iterations this step 7 / 20"
    assert per_step["p"] == pytest.approx(0.35)
    converged = iter_bar(1, 482, 0, None, True, False)
    assert converged["p"] == 1.0
    assert converged["label"] == "Converged at 482"


def feed_series(tracker: ConvTracker, values: list[dict[str, float]]) -> dict:
    result: dict = {}
    for it, residuals in enumerate(values, start=1):
        result = tracker.update(it, residuals)
    return result


def test_plateau_gives_half_at_three_decades() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    series = [{"continuity": 1.0}] + [{"continuity": 1e-3}] * 99
    result = feed_series(tracker, series)
    assert result["p"] == pytest.approx(0.5, abs=1e-6)
    assert result["decades"] == pytest.approx(3.0, abs=1e-6)
    assert result["of"] == pytest.approx(6.0)
    assert result["limiting"] == "continuity"


def test_rising_series_is_rising_with_zero_p() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    series = [{"continuity": 1e-3}] * 5 + [{"continuity": 1e-2}] * 3
    result = feed_series(tracker, series)
    assert result["state"] == "rising"
    assert result["p"] == 0.0


def test_flat_series_is_stalled_without_iters_to_target() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    result = feed_series(tracker, [{"continuity": 1e-3}] * 300)
    assert result["state"] == "stalled"
    assert "iters_to_target" not in result


def test_decay_slope_gives_exact_iterations() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    values = [{"continuity": 10 ** (-0.01 * k)} for k in range(300)]
    result = feed_series(tracker, values)
    exact = math.ceil((-0.01 * 299 + 6) / 0.01)
    assert abs(result["iters_to_target"] - exact) <= 2
    assert result["state"] == "ok"


def test_nan_names_its_column() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    result = tracker.update(1, {"continuity": 1e-3, "x-velocity": math.nan})
    assert result["state"] == "nan"
    assert result["limiting"] == "x-velocity"


def test_excluded_column_is_ignored() -> None:
    tracker = ConvTracker(1e-6, ["continuity"])
    tracker.reset(5)
    result = tracker.update(1, {"continuity": 1.0, "k": 1e-3})
    assert result["limiting"] == "k"
    assert result["rmax"] == pytest.approx(1e-3)


def test_no_included_columns() -> None:
    tracker = ConvTracker(1e-6, ["continuity"])
    tracker.reset(5)
    result = tracker.update(1, {"continuity": 1.0})
    assert result["p"] == 0.0
    assert result["limiting"] is None
    assert result["state"] == "ok"


def test_zero_residual_uses_tiny_floor() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    result = tracker.update(1, {"continuity": 0.0})
    assert result["rmax"] == 1e-300


def test_reference_at_or_below_target_gives_full_bar() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    result = tracker.update(1, {"continuity": 1e-7})
    assert result["of"] == 0.0
    assert result["p"] == 1.0


def test_single_point_has_no_slope() -> None:
    tracker = ConvTracker(1e-6, [])
    tracker.reset(5)
    result = tracker.update(1, {"continuity": 1e-3})
    assert "iters_to_target" not in result


def test_step_tracker_step_120_example() -> None:
    tracker = StepTracker()
    tracker.update(0.0, 119, 0.0119, 881)
    result = tracker.update(5.0, 120, 0.012, 880)
    assert result["label"].startswith("Step 120 · 880 left · t = 0.0120 / 0.1000 s")
    assert result["t_end"] == pytest.approx(0.1)
    assert result["eta_s"] == pytest.approx(5.0 * 880)
    assert result["p"] == pytest.approx(2 / 882)


def test_step_tracker_label_without_timing() -> None:
    tracker = StepTracker()
    result = tracker.update(0.0, 1, 0.001, 9)
    assert result["label"] == "Step 1 · 9 left"
    assert result["t_end"] is None
    assert result["eta_s"] is None


def test_step_tracker_reset_clears_state() -> None:
    tracker = StepTracker()
    tracker.update(0.0, 1, 0.001, 9)
    tracker.update(5.0, 2, 0.002, 8)
    tracker.reset()
    result = tracker.update(100.0, 50, 0.05, 50)
    assert result["p"] == pytest.approx(1 / 51)
    assert result["eta_s"] is None
