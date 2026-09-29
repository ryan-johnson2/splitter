"""Track geometry recovered from telemetry: where the gates are and how the path bends.

The game never sends gate coordinates, but the drone's position at each crossing
time is in the IMU trace. Averaged over runs that gives a stable point per
lap-relative gate, and from consecutive points the spacing and the heading change
at each gate — the raw material for suggesting sections (``core/sections.py``)
and for drawing the track.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol


class Positioned(Protocol):
    @property
    def t_ms(self) -> int: ...

    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...

    @property
    def z(self) -> float: ...


Point = tuple[float, float, float]


def position_at(samples: Sequence[Positioned], t_ms: int, *, max_gap_ms: int = 400) -> Point | None:
    """Linear interpolation of the path at race time ``t_ms``.

    ``None`` outside the trace or when the bracketing samples are further apart
    than ``max_gap_ms`` (a hole in the feed, not a position).
    """
    if not samples:
        return None
    times = [s.t_ms for s in samples]
    i = bisect_left(times, t_ms)
    if i < len(samples) and times[i] == t_ms:
        s = samples[i]
        return (s.x, s.y, s.z)
    if i == 0 or i >= len(samples):
        return None
    a, b = samples[i - 1], samples[i]
    if b.t_ms - a.t_ms > max_gap_ms:
        return None
    f = (t_ms - a.t_ms) / (b.t_ms - a.t_ms)
    return (a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f, a.z + (b.z - a.z) * f)


def heading_at(samples: Sequence[Positioned], t_ms: int, *, span_ms: int = 40) -> Point | None:
    """Unit direction of travel at race time ``t_ms`` (from the path just before
    and after it), or ``None`` where the path is not known on both sides."""
    a, b = position_at(samples, t_ms - span_ms), position_at(samples, t_ms + span_ms)
    if a is None or b is None:
        return None
    v = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-6 else None


@dataclass(frozen=True)
class GatePosition:
    k: int  # lap-relative segment index (1-based), see core/sections.py
    x: float
    y: float
    z: float
    samples: int  # crossings that contributed
    # Mean direction of travel through the gate (unit vector; all zero when
    # unknown). With the position it gives the gate's plane (core/pathtiming.py).
    hx: float = 0.0
    hy: float = 0.0
    hz: float = 0.0


def gate_positions(
    runs: Iterable[tuple[Sequence[Positioned], Iterable[tuple[int, int]]]],
) -> dict[int, GatePosition]:
    """Average position per lap-relative gate across runs.

    Each run is ``(trace, crossings)`` with crossings as ``(k, cumulative_ms)``.
    """
    acc: dict[int, list[Point]] = {}
    heads: dict[int, list[Point]] = {}
    for trace, crossings in runs:
        for k, t_ms in crossings:
            p = position_at(trace, t_ms)
            if p is not None:
                acc.setdefault(k, []).append(p)
                h = heading_at(trace, t_ms)
                if h is not None:
                    heads.setdefault(k, []).append(h)
    out: dict[int, GatePosition] = {}
    for k, points in acc.items():
        n = len(points)
        hx = hy = hz = 0.0
        if heads.get(k):
            hs = heads[k]
            sx, sy, sz = (sum(h[0] for h in hs), sum(h[1] for h in hs), sum(h[2] for h in hs))
            norm = math.sqrt(sx * sx + sy * sy + sz * sz)
            if norm > 1e-6:
                hx, hy, hz = sx / norm, sy / norm, sz / norm
        out[k] = GatePosition(
            k=k,
            x=sum(p[0] for p in points) / n,
            y=sum(p[1] for p in points) / n,
            z=sum(p[2] for p in points) / n,
            samples=n,
            hx=hx,
            hy=hy,
            hz=hz,
        )
    return out


@dataclass(frozen=True)
class GateGeometry:
    k: int
    spacing_m: float | None  # straight-line distance from the previous gate
    heading_change_deg: float | None  # turn at this gate (0 = straight on, 180 = U-turn)


def _horizontal(a: GatePosition, b: GatePosition) -> tuple[float, float]:
    # Unity: y is up, so the top-down plane is (x, z).
    return (b.x - a.x, b.z - a.z)


def _angle_between(u: tuple[float, float], v: tuple[float, float]) -> float | None:
    nu, nv = math.hypot(*u), math.hypot(*v)
    if nu < 1e-6 or nv < 1e-6:
        return None
    cos = max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1]) / (nu * nv)))
    return math.degrees(math.acos(cos))


def gate_geometry(positions: dict[int, GatePosition], count: int) -> list[GateGeometry]:
    """Spacing and heading change for gates ``1..count``; a lap is a loop, so gate 1
    follows gate ``count``. Gates without a position get ``None`` values."""
    out: list[GateGeometry] = []
    for k in range(1, count + 1):
        prev_k = count if k == 1 else k - 1
        next_k = 1 if k == count else k + 1
        here, prev, nxt = positions.get(k), positions.get(prev_k), positions.get(next_k)
        spacing = None
        heading = None
        if here and prev:
            spacing = math.dist((prev.x, prev.y, prev.z), (here.x, here.y, here.z))
            if nxt:
                heading = _angle_between(_horizontal(prev, here), _horizontal(here, nxt))
        out.append(GateGeometry(k=k, spacing_m=spacing, heading_change_deg=heading))
    return out
