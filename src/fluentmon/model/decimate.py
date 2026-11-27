from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence


def decimate(iters: Sequence[int], columns: Sequence[Sequence[float]], max_points: int) -> list[int]:
    if max_points < 4:
        raise ValueError("max_points must be at least 4")
    n = len(iters)
    if n <= max_points:
        return list(range(n))
    valid = [[i for i, value in enumerate(column) if value == value] for column in columns]
    buckets = max_points // 2
    while True:
        keep = {0, n - 1}
        for column, indices in zip(columns, valid):
            for b in range(buckets):
                lo = b * n // buckets
                hi = (b + 1) * n // buckets
                start = bisect_left(indices, lo)
                stop = bisect_left(indices, hi)
                if start == stop:
                    continue
                window = indices[start:stop]
                keep.add(min(window, key=column.__getitem__))
                keep.add(max(window, key=column.__getitem__))
        if len(keep) <= max_points:
            return sorted(keep)
        if buckets == 1:
            ordered = sorted(keep)
            return [ordered[0], *ordered[1:-1][: max_points - 2], ordered[-1]]
        buckets = max(1, buckets // 2)
