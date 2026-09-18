"""Does this run look like the track the session says it is?

Single player never names the track, so the session is carried over from the
last run and a pilot who switched tracks in the game silently records runs
(and PBs) against the wrong one. The trace tells the truth: after the first
lap we know how many gates the lap has and roughly where each gate is, and
both can be compared with what earlier runs on the session's track showed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from splitter.core.geometry import GatePosition, Point

# Different tracks in the same scenery can share the start area, and a pilot
# passes a gate a few metres off between runs; well beyond that it is not the
# same gate. Measured on the trial track: gate spacing 2-27 m, median 5 m.
DIFFERENT_M = 12.0
MIN_GATES = 4


@dataclass(frozen=True)
class TrackCheck:
    verdict: str  # same | different | unknown
    confidence: str  # high | medium | -
    reason: str
    gates_compared: int = 0
    mean_distance_m: float | None = None


def compare(
    run_gates_per_lap: int | None,
    run_positions: Mapping[int, Point],
    ref_gates_per_lap: int | None,
    ref_positions: Mapping[int, GatePosition],
) -> TrackCheck:
    """Compare one lap of a run with what is known about the session's track.

    ``run_positions`` and ``ref_positions`` are keyed by lap-relative segment.
    A gate-count mismatch is a different track with high confidence; the
    geometry test needs at least ``MIN_GATES`` gates in common.
    """
    if run_gates_per_lap and ref_gates_per_lap and run_gates_per_lap != ref_gates_per_lap:
        return TrackCheck(
            "different",
            "high",
            f"{run_gates_per_lap} gates per lap here, {ref_gates_per_lap} on that track",
        )
    common = [k for k in run_positions if k in ref_positions]
    if len(common) < MIN_GATES:
        return TrackCheck("unknown", "-", "not enough gate positions to compare", len(common))
    dists = []
    for k in common:
        p, r = run_positions[k], ref_positions[k]
        dists.append(math.dist(p, (r.x, r.y, r.z)))
    mean = sum(dists) / len(dists)
    if mean >= DIFFERENT_M:
        return TrackCheck(
            "different",
            "medium",
            f"gates are {mean:.0f} m from where that track's gates are",
            len(common),
            round(mean, 1),
        )
    return TrackCheck("same", "high", "gates match", len(common), round(mean, 1))
