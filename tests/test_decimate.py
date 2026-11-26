from __future__ import annotations

import math
import random

import pytest

from fluentmon.model.decimate import decimate


def test_short_series_keeps_everything() -> None:
    iters = list(range(10))
    assert decimate(iters, [[1.0] * 10], 10) == list(range(10))


def test_spike_survives_and_ends_are_kept() -> None:
    n = 50_000
    rng = random.Random(1)
    iters = list(range(1, n + 1))
    columns = [[rng.random() for _ in range(n)] for _ in range(10)]
    spike = 31_234
    columns[3][spike] = 1e9
    kept = decimate(iters, columns, 1500)
    assert len(kept) <= 1500
    assert spike in kept
    assert kept[0] == 0
    assert kept[-1] == n - 1
    assert kept == sorted(set(kept))


def test_all_nan_column_is_ignored() -> None:
    n = 5000
    nan_column = [math.nan] * n
    ramp = [float(i) for i in range(n)]
    kept = decimate(list(range(n)), [nan_column, ramp], 100)
    assert len(kept) <= 100
    assert 0 in kept and n - 1 in kept


def test_exactly_max_points_keeps_all() -> None:
    iters = list(range(1500))
    assert decimate(iters, [[float(i) for i in iters]], 1500) == iters


def test_max_points_below_four_is_refused() -> None:
    with pytest.raises(ValueError):
        decimate([1, 2, 3, 4, 5], [[1.0, 2.0, 3.0, 4.0, 5.0]], 3)


def test_empty_series() -> None:
    assert decimate([], [[]], 10) == []
