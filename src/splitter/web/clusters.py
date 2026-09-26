"""Group unidentified runs by layout, so a pilot labels a track once, not run by run.

Greedy and deterministic: runs in order of first flight, each joining the
first cluster with the same gate count whose centroid is within the
track-change threshold (``core/trackcheck.py``), else starting a new one.
The centroid is the running mean of the located gate positions and is what
gets registered when the cluster is labelled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from splitter.core import trackcheck
from splitter.core.fingerprint import Fingerprint
from splitter.core.geometry import Point


@dataclass(frozen=True)
class Member:
    race_id: int
    started_at: datetime
    node_id: str
    fingerprint: Fingerprint


@dataclass
class Cluster:
    gates_per_lap: int
    members: list[Member] = field(default_factory=list)
    _sums: dict[int, list[float]] = field(default_factory=dict)
    _counts: dict[int, int] = field(default_factory=dict)

    @property
    def centroid(self) -> Fingerprint:
        gates: list[Point | None] = []
        for k in range(1, self.gates_per_lap + 1):
            n = self._counts.get(k, 0)
            s = self._sums.get(k)
            gates.append((s[0] / n, s[1] / n, s[2] / n) if n and s else None)
        return Fingerprint(self.gates_per_lap, 0, tuple(gates))

    @property
    def nodes(self) -> set[str]:
        return {m.node_id for m in self.members}

    @property
    def first_seen(self) -> datetime:
        return min(m.started_at for m in self.members)

    @property
    def last_seen(self) -> datetime:
        return max(m.started_at for m in self.members)

    def distance(self, fp: Fingerprint) -> float | None:
        mine = self.centroid.positions
        common = [k for k in fp.positions if k in mine]
        if len(common) < trackcheck.MIN_GATES:
            return None
        return sum(math.dist(fp.positions[k], mine[k]) for k in common) / len(common)

    def add(self, m: Member) -> None:
        self.members.append(m)
        for k, p in m.fingerprint.positions.items():
            s = self._sums.setdefault(k, [0.0, 0.0, 0.0])
            s[0] += p[0]
            s[1] += p[1]
            s[2] += p[2]
            self._counts[k] = self._counts.get(k, 0) + 1


def cluster(members: list[Member], threshold_m: float = trackcheck.DIFFERENT_M) -> list[Cluster]:
    """Clusters ordered by first flight; members inside keep their order."""
    out: list[Cluster] = []
    for m in sorted(members, key=lambda x: (x.started_at, x.race_id)):
        fp = m.fingerprint
        if fp.located < trackcheck.MIN_GATES:
            continue
        home = None
        for c in out:
            if c.gates_per_lap != fp.gates_per_lap:
                continue
            d = c.distance(fp)
            if d is not None and d < threshold_m:
                home = c
                break
        if home is None:
            home = Cluster(fp.gates_per_lap)
            out.append(home)
        home.add(m)
    return out
