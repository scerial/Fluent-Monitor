from __future__ import annotations

import math
from collections import deque
from collections.abc import Collection, Mapping, Sequence

TINY = 1e-300
SLOPE_EPS = -1e-4
SLOPE_WINDOW = 200
STALL_MIN_POINTS = 10
EMA_NEW = 0.2


def fmt_hms(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def iter_bar(
    i0: int,
    i: int,
    left: int,
    eta_s: int | None,
    converged: bool,
    per_step: bool,
) -> dict:
    done = i - i0 + 1
    total = done + left
    p = done / total
    if converged:
        p = 1.0
        label = f"Converged at {i}"
    elif per_step:
        label = f"Iterations this step {done} / {total}"
    else:
        label = f"Iteration {done} / {total} · {left} left"
        if eta_s is not None:
            label += f" · ETA {fmt_hms(eta_s)}"
    return {"p": p, "done": done, "total": total, "left": left, "eta_s": eta_s, "label": label}


def _slope(points: Sequence[tuple[int, float]]) -> float | None:
    count = len(points)
    if count < 2:
        return None
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return sxy / sxx


class ConvTracker:
    def __init__(self, target: float, exclude: Collection[str]) -> None:
        self.target = target
        self.exclude = set(exclude)
        self.ref_window = 5
        self._count = 0
        self._ref: float | None = None
        self._smoothed: float | None = None
        self._points: deque[tuple[int, float]] = deque(maxlen=SLOPE_WINDOW)

    def reset(self, ref_window: int) -> None:
        self.ref_window = max(1, ref_window)
        self._count = 0
        self._ref = None
        self._smoothed = None
        self._points.clear()

    def update(self, it: int, residuals: Mapping[str, float]) -> dict:
        included = {name: value for name, value in residuals.items() if name not in self.exclude}
        nan_names = [name for name, value in included.items() if math.isnan(value)]
        valid = {name: value for name, value in included.items() if not math.isnan(value)}

        if not included:
            return {
                "p": 0.0,
                "rmax": math.nan,
                "limiting": None,
                "decades": 0.0,
                "of": 0.0,
                "state": "ok",
            }

        if not valid:
            return {
                "p": 0.0,
                "rmax": math.nan,
                "limiting": nan_names[0],
                "decades": 0.0,
                "of": 0.0,
                "state": "nan",
            }

        limiting = max(valid, key=lambda name: valid[name])
        rmax = max(valid[limiting], TINY)
        log_rmax = math.log10(rmax)

        self._count += 1
        if self._count <= self.ref_window:
            self._ref = rmax if self._ref is None else max(self._ref, rmax)
        assert self._ref is not None
        r_ref = self._ref

        if self._smoothed is None:
            self._smoothed = log_rmax
        else:
            self._smoothed = (1 - EMA_NEW) * self._smoothed + EMA_NEW * log_rmax

        self._points.append((it, log_rmax))
        slope = _slope(self._points)
        log_ref = math.log10(r_ref)
        decades = log_ref - self._smoothed
        of = log_ref - math.log10(self.target)
        if of <= 0:
            of = 0.0
            p = 1.0
        else:
            p = min(max(decades / of, 0.0), 1.0)

        result: dict = {
            "p": p,
            "rmax": rmax,
            "limiting": limiting,
            "decades": decades,
            "of": of,
        }
        if slope is not None and slope < SLOPE_EPS:
            result["iters_to_target"] = max(
                0, math.ceil((log_rmax - math.log10(self.target)) / -slope)
            )

        if nan_names:
            result["state"] = "nan"
            result["limiting"] = nan_names[0]
        elif rmax > r_ref:
            result["state"] = "rising"
            result["p"] = 0.0
        elif (
            slope is not None
            and slope >= SLOPE_EPS
            and len(self._points) >= STALL_MIN_POINTS
        ):
            result["state"] = "stalled"
        else:
            result["state"] = "ok"
        return result


class StepTracker:
    def __init__(self, window: int = 10) -> None:
        self.window = window
        self._gaps: deque[float] = deque(maxlen=window)
        self.reset()

    def reset(self) -> None:
        self._n0: int | None = None
        self._last_now: float | None = None
        self._last_step: int | None = None
        self._last_flow: float | None = None
        self._dt: float | None = None
        self._gaps.clear()

    def update(self, now: float, step: int, flow_time: float, more: int) -> dict:
        if self._n0 is None:
            self._n0 = step
        if self._last_now is not None and now - self._last_now > 0:
            self._gaps.append(now - self._last_now)
        if self._last_step is not None and self._last_flow is not None and step > self._last_step:
            self._dt = (flow_time - self._last_flow) / (step - self._last_step)
        self._last_now = now
        self._last_step = step
        self._last_flow = flow_time

        done = step - self._n0 + 1
        p = done / (done + more)
        wall = sum(self._gaps) / len(self._gaps) if self._gaps else None
        eta_s = wall * more if wall is not None else None
        t_end = flow_time + more * self._dt if self._dt is not None else None

        label = f"Step {step} · {more} left"
        if t_end is not None:
            label += f" · t = {flow_time:.4f} / {t_end:.4f} s"
        if eta_s is not None:
            label += f" · ETA {fmt_hms(eta_s)}"
        return {
            "p": p,
            "n": step,
            "left": more,
            "flow_time": flow_time,
            "t_end": t_end,
            "eta_s": eta_s,
            "label": label,
        }
