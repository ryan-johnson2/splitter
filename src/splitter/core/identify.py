"""Which known layout is this run? Pure ranking over the fingerprint registry.

The web sees runs from nodes that never picked a track (single player never
names it). A run's fingerprint (``core/fingerprint.py``) is ranked against
every registry row with the same gate count using the track-change check's
thresholds (``core/trackcheck.py``); one clear winner attributes the run,
twins (the same layout under two track ids) are flagged for the pilot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from splitter.core import trackcheck
from splitter.core.fingerprint import Fingerprint
from splitter.core.geometry import GatePosition


@dataclass(frozen=True)
class Known:
    """One registry entry, already reduced to geometry."""

    track_id: int
    scene_id: int
    track_name: str
    scenery: str
    track_source: str
    gates_per_lap: int
    positions: dict[int, GatePosition]


@dataclass(frozen=True)
class Decision:
    track: Known | None  # attribute to this
    ambiguous: bool  # twins: attributed to the most recent, flag it
    candidates: list[tuple[Known, float]]  # every match, best first, with mean distance


def known_from_fingerprint(
    track_id: int,
    scene_id: int,
    track_name: str,
    scenery: str,
    track_source: str,
    fp: Fingerprint,
) -> Known:
    positions = {k: GatePosition(k, p[0], p[1], p[2], 1) for k, p in fp.positions.items()}
    return Known(track_id, scene_id, track_name, scenery, track_source, fp.gates_per_lap, positions)


def identify(fp: Fingerprint | None, registry: Mapping[int, Known]) -> Decision:
    """Rank ``fp`` against ``registry`` (track id → geometry).

    A clear winner (runner-up ≥ ``trackcheck.CLOSE_M`` worse) is attributed.
    Several within ``CLOSE_M`` of each other are twins: the first (the caller
    orders the registry most recently used first) is attributed and the run is
    flagged ambiguous. No fingerprint or no match: nothing.
    """
    if fp is None or not registry:
        return Decision(None, False, [])
    ranked = trackcheck.rank(
        fp.gates_per_lap,
        fp.positions,
        {tid: (k.gates_per_lap, k.positions) for tid, k in registry.items()},
    )
    candidates = [(registry[c.track_id], c.mean_distance_m) for c in ranked]
    if not ranked:
        return Decision(None, False, candidates)
    pick = trackcheck.decide(ranked)
    if pick is not None:
        return Decision(registry[pick.track_id], False, candidates)
    # Twins: everything within CLOSE_M of the best; the registry's order decides.
    best = ranked[0].mean_distance_m
    twins = [c for c in ranked if c.mean_distance_m - best < trackcheck.CLOSE_M]
    order = list(registry)
    twins.sort(key=lambda c: order.index(c.track_id))
    return Decision(registry[twins[0].track_id], True, candidates)
