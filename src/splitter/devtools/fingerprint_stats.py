"""Threshold spike: do fingerprints separate the tracks in this database?

``splitter fingerprint-stats`` compares every pair of fingerprinted runs with
the same gate count: mean gate distance for pairs on the same track versus
pairs on different tracks, and how many fall on the wrong side of
``trackcheck.DIFFERENT_M``. Run it on a database with real runs before
trusting the threshold for server-side identification (docs/sync-plan.md).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from itertools import combinations
from typing import Any

from sqlalchemy import select

from splitter.core import trackcheck
from splitter.core.fingerprint import Fingerprint
from splitter.db.models import Race


def mean_distance(a: Fingerprint, b: Fingerprint) -> float | None:
    """Mean distance over gates both fingerprints located; None below ``MIN_GATES``."""
    pa, pb = a.positions, b.positions
    common = [k for k in pa if k in pb]
    if len(common) < trackcheck.MIN_GATES:
        return None
    return sum(math.dist(pa[k], pb[k]) for k in common) / len(common)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))]


def summarise(
    runs: list[tuple[int, Fingerprint]],
) -> dict[str, Any]:
    """``runs`` = (track_id, fingerprint) for identified runs. Pure, for tests."""
    same: list[float] = []
    cross: list[float] = []
    by_count: dict[int, list[tuple[int, Fingerprint]]] = defaultdict(list)
    for track_id, fp in runs:
        by_count[fp.gates_per_lap].append((track_id, fp))
    for group in by_count.values():
        for (ta, fa), (tb, fb) in combinations(group, 2):
            d = mean_distance(fa, fb)
            if d is None:
                continue
            (same if ta == tb else cross).append(d)
    thr = trackcheck.DIFFERENT_M
    out: dict[str, Any] = {
        "runs": len(runs),
        "tracks": len({t for t, _ in runs}),
        "same_pairs": len(same),
        "cross_pairs": len(cross),
        "threshold_m": thr,
        "same_over_threshold": sum(1 for d in same if d >= thr),
        "cross_under_threshold": sum(1 for d in cross if d < thr),
    }
    if same:
        out["same"] = {
            "median": _percentile(same, 0.5),
            "p95": _percentile(same, 0.95),
            "max": max(same),
        }
    if cross:
        out["cross"] = {
            "min": min(cross),
            "p5": _percentile(cross, 0.05),
            "median": _percentile(cross, 0.5),
        }
    return out


async def run_stats(session_factory: Any) -> None:
    if hasattr(session_factory, "__await__"):
        session_factory = await session_factory
    async with session_factory() as db:
        stmt = select(Race).where((Race.track_id > 0) & (Race.fingerprint != ""))
        rows = (await db.execute(stmt)).scalars().all()
        runs = []
        for r in rows:
            fp = Fingerprint.from_dict(json.loads(r.fingerprint))
            if fp is not None:
                runs.append((r.track_id, fp))
    s = summarise(runs)
    print(f"{s['runs']} fingerprinted runs on {s['tracks']} tracks")
    print(f"threshold {s['threshold_m']} m (trackcheck.DIFFERENT_M)")
    if "same" in s:
        d = s["same"]
        print(
            f"same track   ({s['same_pairs']} pairs): median {d['median']:.1f} m, "
            f"p95 {d['p95']:.1f} m, max {d['max']:.1f} m, "
            f"{s['same_over_threshold']} pairs over the threshold"
        )
    if "cross" in s:
        d = s["cross"]
        print(
            f"other tracks ({s['cross_pairs']} pairs): min {d['min']:.1f} m, "
            f"p5 {d['p5']:.1f} m, median {d['median']:.1f} m, "
            f"{s['cross_under_threshold']} pairs under the threshold"
        )
    if s["runs"] < 2:
        print(
            "nothing to compare yet: fewer than two fingerprinted runs "
            "(`splitter backfill-fingerprints` fingerprints older traced runs)"
        )
