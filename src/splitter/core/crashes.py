"""Crash detection from the IMU trace.

The game sends no crash or respawn event, and a pilot often flies on after a
crash, so crashes are found as *events in the trace* of every run, finished or
aborted. Measured on real runs (2026-09-13, 20 Hz stored trace): a crash drops the
speed from ~20 to ~3 m/s within one 50 ms step (-130 to -320 m/s^2) with a gyro
spike of 700 to 1170, while hard braking stays around -60 to -85 m/s^2 and the gyro's
99th percentile sits near 570. The thresholds below live in that gap.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol


class Traced(Protocol):
    @property
    def t_ms(self) -> int: ...

    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...

    @property
    def z(self) -> float: ...

    @property
    def speed(self) -> float: ...

    @property
    def roll(self) -> float: ...

    @property
    def pitch(self) -> float: ...

    @property
    def yaw(self) -> float: ...


@dataclass(frozen=True)
class Thresholds:
    decel: float = -120.0  # m/s^2 over one sample step
    loss_fraction: float = 0.7  # share of speed lost within loss_window_ms …
    loss_decel: float = -80.0  # … and that loss must still be this hard (no slow bumps)
    loss_window_ms: int = 100
    min_speed: float = 8.0  # only count drops from a real flying speed
    gyro_percentile: float = 0.95  # confirm with a gyro spike above the run's own …
    gyro_floor: float = 300.0  # … but never below this (a calm run has no spikes)
    confirm_window_ms: int = 150
    dwell_speed: float = 3.0  # or confirm with a stop: below this …
    dwell_ms: int = 300  # … for this long after the drop
    merge_ms: int = 1500  # bounces this close to a crash are the same crash


DEFAULT = Thresholds()


@dataclass(frozen=True)
class Crash:
    t_ms: int
    x: float
    y: float
    z: float
    speed_before: float  # m/s
    decel: float  # m/s^2, most negative of the step and the window estimate
    gyro: float  # peak gyro magnitude around the event
    lap: int = 0  # filled by attribute(); 0 = before the first lap started
    segment: int = 0  # lap-relative segment (1-based) the crash happened in; 0 = holeshot

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("x", "y", "z", "speed_before", "decel", "gyro"):
            d[key] = round(d[key], 2)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Crash:
        return cls(**{k: d.get(k, 0) for k in cls.__dataclass_fields__})


def _gyro(s: Traced) -> float:
    return math.sqrt(s.roll * s.roll + s.pitch * s.pitch + s.yaw * s.yaw)


def detect(samples: Sequence[Traced], th: Thresholds = DEFAULT) -> list[Crash]:
    """Find crashes in a time-ordered trace (any sample rate from 20 Hz up)."""
    n = len(samples)
    if n < 3:
        return []
    gyro = [_gyro(s) for s in samples]
    ranked = sorted(gyro)
    g_thr = max(th.gyro_floor, ranked[min(n - 1, int(th.gyro_percentile * (n - 1)))])
    crashes: list[Crash] = []
    j = 0  # first sample at or after t + loss_window_ms (monotonic in i)
    for i in range(n - 1):
        a, b = samples[i], samples[i + 1]
        if a.speed < th.min_speed:
            continue
        dt = (b.t_ms - a.t_ms) / 1000.0
        if dt <= 0 or dt > 0.5:
            continue
        step_acc = (b.speed - a.speed) / dt
        while j < n and samples[j].t_ms < a.t_ms + th.loss_window_ms:
            j += 1
        loss = None
        window_acc = 0.0
        if j < n and samples[j].t_ms - a.t_ms <= th.loss_window_ms * 3:
            loss = (a.speed - samples[j].speed) / a.speed
            window_acc = (samples[j].speed - a.speed) / ((samples[j].t_ms - a.t_ms) / 1000.0)
        by_loss = loss is not None and loss >= th.loss_fraction and window_acc <= th.loss_decel
        if not (step_acc <= th.decel or by_loss):
            continue
        # Pin the event to the sample right before the steepest step of the drop.
        m = i
        for cand in range(i, max(i, j - 1) + 1):
            if cand + 1 < n and (samples[cand].speed - samples[cand + 1].speed) > (
                samples[m].speed - samples[m + 1].speed
            ):
                m = cand
        a = samples[m]
        if crashes and a.t_ms - crashes[-1].t_ms < th.merge_ms:
            continue
        lo, hi = a.t_ms - th.confirm_window_ms, a.t_ms + th.confirm_window_ms
        k = m
        while k > 0 and samples[k - 1].t_ms >= lo:
            k -= 1
        g_peak = 0.0
        while k < n and samples[k].t_ms <= hi:
            g_peak = max(g_peak, gyro[k])
            k += 1
        confirmed = g_peak >= g_thr
        if not confirmed:
            start, end = a.t_ms + th.loss_window_ms, a.t_ms + th.loss_window_ms + th.dwell_ms
            dwell = [s for s in samples[m + 1 :] if start <= s.t_ms <= end]
            confirmed = bool(dwell) and all(s.speed < th.dwell_speed for s in dwell)
        if not confirmed:
            continue
        crashes.append(
            Crash(
                t_ms=a.t_ms,
                x=a.x,
                y=a.y,
                z=a.z,
                speed_before=a.speed,
                decel=min(step_acc, window_acc),
                gyro=g_peak,
            )
        )
    return crashes


class CrossingLike(Protocol):
    @property
    def seq(self) -> int: ...

    @property
    def lap(self) -> int: ...

    @property
    def ends_lap(self) -> int | None: ...

    @property
    def cumulative_ms(self) -> int: ...


def attribute(crashes: Sequence[Crash], crossings: Sequence[CrossingLike]) -> list[Crash]:
    """Stamp each crash with the lap and lap-relative segment it happened in."""
    from splitter.core.sections import segment_at

    out: list[Crash] = []
    for c in crashes:
        lap, segment = segment_at(crossings, c.t_ms)
        out.append(replace(c, lap=lap, segment=segment))
    return out
