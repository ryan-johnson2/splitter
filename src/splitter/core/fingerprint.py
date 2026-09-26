"""A run's layout fingerprint: gates per lap and where the gates are.

The game never names the track in single player, but the drone's position at
each crossing is in the IMU trace. One lap of those positions, keyed by
lap-relative gate, is enough to tell layouts apart (``core/trackcheck.py``)
and is what a web that never saw the live session identifies a run by.
The fingerprint is taken from the fastest lap with no crash (a lap-1 crash
puts gates in odd places), lap 1 when every lap crashed, and stored on the
race so it survives without the trace.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from splitter.core import geometry, sections
from splitter.core.geometry import Point, Positioned

VERSION = 1


@dataclass(frozen=True)
class Fingerprint:
    gates_per_lap: int
    lap: int  # which lap the positions came from
    gates: tuple[Point | None, ...]  # index k-1 = lap-relative gate k; None = no position

    @property
    def positions(self) -> dict[int, Point]:
        return {k: p for k, p in enumerate(self.gates, start=1) if p is not None}

    @property
    def located(self) -> int:
        return sum(1 for p in self.gates if p is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "gates_per_lap": self.gates_per_lap,
            "lap": self.lap,
            "gates": [
                [round(p[0], 2), round(p[1], 2), round(p[2], 2)] if p else None for p in self.gates
            ],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Fingerprint | None:
        if not d or not d.get("gates_per_lap"):
            return None
        gates = tuple(
            (float(g[0]), float(g[1]), float(g[2])) if g else None for g in d.get("gates", [])
        )
        return cls(int(d["gates_per_lap"]), int(d.get("lap", 1)), gates)


def compute(
    crossings: Sequence[sections.Crossing],
    gates_per_lap: int | None,
    samples: Sequence[Positioned],
    lap_times: Iterable[tuple[int, int]],
    crashed_laps: Iterable[int] = (),
) -> Fingerprint | None:
    """Fingerprint a run from its crossings and trace.

    ``lap_times`` is ``(lap, lap_ms)`` for every completed lap; ``crashed_laps``
    the laps a crash was attributed to. ``None`` without a gate count, a trace,
    or a complete lap.
    """
    if not gates_per_lap or not samples:
        return None
    by_lap = sections.lap_segments(crossings)
    complete = {lap for lap, segs in by_lap.items() if len(segs) == gates_per_lap}
    if not complete:
        return None
    crashed = set(crashed_laps)
    clean = [(ms, lap) for lap, ms in lap_times if lap in complete and lap not in crashed]
    lap = min(clean)[1] if clean else min(complete)
    gates: list[Point | None] = []
    for c in by_lap[lap]:
        gates.append(geometry.position_at(samples, c.cumulative_ms))
    fp = Fingerprint(gates_per_lap, lap, tuple(gates))
    return fp if fp.located else None
