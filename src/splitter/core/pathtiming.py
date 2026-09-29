"""Gate crossings from the flight path, for runs the game sent no ``racedata`` for.

On 2026-09-29 the game kept its websocket up, streamed IMU at 60 Hz and
announced ``race finished`` for run after run, but emitted no ``racedata`` for
about half of them: twenty minutes of passes with no gate crossings. Nothing
on the consumer side can bring those frames back — but the flight path is in
the IMU stream, and where the gates are is known from earlier traced runs on
the track (``core/geometry.py``). So a crossing is *detected*: the first pass
through a gate's plane (its averaged crossing position and heading), within
:data:`RADIUS_M` of that point, gates taken strictly in lap order.

Measured against the game's own crossings on six real 3-lap runs (24 gates,
leave-one-out gate model, 20 Hz stored traces): every crossing found in
order, lap times within 11 ms, totals within 15 ms, median gate error 16 ms.
One gate on that track is picked up about a second early on an approach pass
that brushes its plane; lap times are unaffected. A crash-respawn can also
put a single gate off by the crash's duration. Good enough to keep a run;
not the game's own timing, and marked as such (``races.timing_source``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from splitter.core.geometry import GatePosition, Positioned

RADIUS_M = 6.0  # how far from the gate's averaged crossing point still counts
GRACE_MS = 1500  # how long the node waits for the game's own crossing before trusting the path


@dataclass(frozen=True)
class GatePlane:
    k: int  # lap-relative gate (segment index); k == count is the start/finish
    x: float
    y: float
    z: float
    nx: float  # unit heading through the gate
    ny: float
    nz: float

    def along(self, p: Positioned) -> float:
        """Signed distance of ``p`` from the plane, positive past the gate."""
        return (p.x - self.x) * self.nx + (p.y - self.y) * self.ny + (p.z - self.z) * self.nz


@dataclass(frozen=True)
class PathCrossing:
    """A detected crossing in the game's wire numbering (see ``core/timing.py``):
    the start/finish is gate 1 of the lap it starts, segment k is gate k + 1,
    the finish carries ``finished`` and gate count + 1."""

    k: int  # 0 = the start/finish crossing that ends the holeshot
    t_ms: int
    lap: int
    gate: int
    finished: bool


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if n < 1e-6:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def model_from_geometry(
    positions: Mapping[int, GatePosition], count: int | None
) -> dict[int, GatePlane] | None:
    """Planes for gates ``1..count`` from averaged gate positions. A position
    without a heading (older bundles, registry rows) gets the direction from
    the previous gate to the next. ``None`` unless every gate is located."""
    if not count or any(k not in positions for k in range(1, count + 1)):
        return None
    out: dict[int, GatePlane] = {}
    for k in range(1, count + 1):
        p = positions[k]
        n = _unit((p.hx, p.hy, p.hz))
        if n is None:
            prev = positions[count if k == 1 else k - 1]
            nxt = positions[1 if k == count else k + 1]
            n = _unit((nxt.x - prev.x, nxt.y - prev.y, nxt.z - prev.z))
        if n is None:
            return None
        out[k] = GatePlane(k, p.x, p.y, p.z, n[0], n[1], n[2])
    return out


class PathDetector:
    """Feed it the trace sample by sample; it yields a crossing whenever the
    path passes through the next gate's plane. Gates are taken strictly in
    lap order: the start/finish first (the holeshot), then 1..count per lap."""

    def __init__(
        self,
        model: Mapping[int, GatePlane],
        count: int,
        race_laps: int = 0,
        radius_m: float = RADIUS_M,
    ) -> None:
        self.model = model
        self.count = count
        self.race_laps = race_laps
        self.radius_m = radius_m
        self.crossings: list[PathCrossing] = []
        self.lap = 0  # lap in progress; 0 during the holeshot
        self.finished = False
        self._prev: Positioned | None = None
        self._last_k: int | None = None
        self._t_last = -1

    @property
    def next_k(self) -> int:
        if self._last_k is None or self._last_k == self.count:
            return self.count if self._last_k is None else 1
        return self._last_k + 1

    def feed(self, s: Positioned) -> PathCrossing | None:
        prev, self._prev = self._prev, s
        if prev is None or self.finished or s.t_ms <= self._t_last:
            return None
        plane = self.model.get(self.next_k)
        if plane is None:
            return None
        before, after = plane.along(prev), plane.along(s)
        if not (before < 0 <= after):
            return None
        f = -before / (after - before)
        at = (
            prev.x + (s.x - prev.x) * f,
            prev.y + (s.y - prev.y) * f,
            prev.z + (s.z - prev.z) * f,
        )
        if math.dist(at, (plane.x, plane.y, plane.z)) > self.radius_m:
            return None
        t_ms = round(prev.t_ms + (s.t_ms - prev.t_ms) * f)
        if t_ms <= 0:
            return None
        return self._take(plane.k, t_ms)

    def _take(self, k: int, t_ms: int) -> PathCrossing:
        first = self._last_k is None
        if k == self.count:
            closing_last = bool(self.race_laps) and self.lap == self.race_laps
            if closing_last:
                c = PathCrossing(k, t_ms, self.lap, self.count + 1, True)
                self.finished = True
            else:
                self.lap += 1
                c = PathCrossing(0 if first else k, t_ms, self.lap, 1, False)
        else:
            c = PathCrossing(k, t_ms, self.lap, k + 1, False)
        self.crossings.append(c)
        self._last_k = k
        self._t_last = t_ms
        return c


def reconstruct(
    samples: Sequence[Positioned],
    model: Mapping[int, GatePlane],
    count: int,
    race_laps: int = 0,
) -> list[PathCrossing]:
    """Every crossing a stored trace shows, in order."""
    det = PathDetector(model, count, race_laps)
    for s in samples:
        det.feed(s)
    return det.crossings
