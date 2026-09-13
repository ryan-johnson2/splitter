"""Track sections: groups of consecutive gates, and the per-section analysis.

Gates are addressed by their **lap-relative segment index** ``k`` (1-based): the
k-th crossing of a lap ends segment ``k``. The last segment of a lap ends at the
start/finish crossing (the game reports that crossing as the next lap's first
gate, or one ordinal past the per-lap count on the finish). The holeshot (GO →
first start/finish crossing) is not a segment. ``lap_segments`` does that
bookkeeping from the stored crossings, so nothing else needs the wire ordinals.

A track's sections cover ``1..count`` contiguously; they are suggested from
geometry and speed (``suggest``) and edited by the pilot.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from splitter.core.geometry import GateGeometry

MAX_SECTIONS = 12
C = TypeVar("C", bound="Crossing")


class Crossing(Protocol):
    @property
    def seq(self) -> int: ...

    @property
    def lap(self) -> int: ...

    @property
    def ends_lap(self) -> int | None: ...

    @property
    def cumulative_ms(self) -> int: ...


class Segment(Crossing, Protocol):
    @property
    def gate(self) -> int: ...

    @property
    def gate_ms(self) -> int: ...

    @property
    def avg_speed(self) -> float | None: ...

    @property
    def min_speed(self) -> float | None: ...

    @property
    def min_accel(self) -> float | None: ...


@dataclass(frozen=True)
class Section:
    ordinal: int
    name: str
    first: int  # segment index, inclusive
    last: int

    def contains(self, k: int) -> bool:
        return self.first <= k <= self.last

    @property
    def size(self) -> int:
        return self.last - self.first + 1

    def to_dict(self) -> dict[str, Any]:
        return {"ordinal": self.ordinal, "name": self.name, "first": self.first, "last": self.last}


# ── lap bookkeeping ───────────────────────────────────────────────


def lap_segments(crossings: Sequence[C]) -> dict[int, list[C]]:
    """Group a run's crossings into ``{lap: [segment 1, segment 2, …]}``.

    Lap-0 crossings and the first lap-1 crossing (the start/finish that ends the
    holeshot) are not segments. A crossing with ``ends_lap`` set is the last
    segment of that lap. A trailing partial lap (aborted run) is kept.
    """
    out: dict[int, list[C]] = {}
    current: int | None = None
    for c in sorted(crossings, key=lambda c: c.seq):
        if c.lap <= 0:
            continue
        if current is None:
            current = c.lap
            continue
        out.setdefault(current, []).append(c)
        if c.ends_lap is not None:
            current = c.ends_lap + 1
    return out


def segment_at(crossings: Sequence[Crossing], t_ms: int) -> tuple[int, int]:
    """``(lap, segment)`` the race clock ``t_ms`` falls in; ``(0, 0)`` during the holeshot."""
    ordered = sorted(crossings, key=lambda c: c.seq)
    last: Crossing | None = None
    for c in ordered:
        if c.cumulative_ms <= t_ms:
            last = c
        else:
            break
    if last is None or last.lap <= 0:
        return (0, 0)
    laps = lap_segments(ordered)
    for lap, segs in laps.items():
        for k, s in enumerate(segs, start=1):
            if s.seq == last.seq:
                return (lap + 1, 1) if s.ends_lap is not None else (lap, k + 1)
    # The holeshot-ending crossing: lap 1 has begun, first segment.
    return (last.lap, 1)


def segment_labels(segments: Sequence[Segment]) -> dict[int, str]:
    """``{k: "G3", …, count: "S/F"}`` from one complete lap's crossings."""
    labels: dict[int, str] = {}
    for k, s in enumerate(segments, start=1):
        labels[k] = "S/F" if s.ends_lap is not None else f"G{s.gate}"
    return labels


# ── configuration ─────────────────────────────────────────────────


def validate(sections: Sequence[Section], count: int) -> list[Section]:
    """Normalise ordinals/names and check contiguous coverage of ``1..count``."""
    if count < 1:
        raise ValueError("track has no gates")
    ordered = sorted(sections, key=lambda s: s.first)
    if not ordered:
        raise ValueError("at least one section is required")
    if len(ordered) > MAX_SECTIONS:
        raise ValueError(f"at most {MAX_SECTIONS} sections")
    expected = 1
    out: list[Section] = []
    for i, s in enumerate(ordered, start=1):
        if s.first != expected or s.last < s.first:
            raise ValueError(f"sections must cover gates 1..{count} without gaps or overlaps")
        name = s.name.strip() or f"Section {i}"
        out.append(Section(ordinal=i, name=name[:40], first=s.first, last=s.last))
        expected = s.last + 1
    if expected != count + 1:
        raise ValueError(f"sections must cover gates 1..{count} without gaps or overlaps")
    return out


def equal_split(count: int, parts: int) -> list[Section]:
    parts = max(1, min(parts, count))
    bounds = [round(count * i / parts) for i in range(parts + 1)]
    return [Section(i, f"Section {i}", bounds[i - 1] + 1, bounds[i]) for i in range(1, parts + 1)]


def suggest(
    count: int,
    geometry: Sequence[GateGeometry] | None = None,
    speeds: Sequence[float | None] | None = None,
    *,
    max_sections: int = 8,
    min_gates: int = 3,
) -> list[Section]:
    """Seed a section layout from what the data shows.

    A boundary before segment ``k`` scores from: the heading change at gate
    ``k-1`` (the path turns, so the approach to ``k`` is a new direction), a
    change of speed regime between the two segments, and a jump in gate spacing.
    The largest section is split at its best-scoring boundary, repeatedly, so
    the layout spreads along the lap instead of clustering where the turns are
    densest; every section stays at least ``min_gates`` long. With nothing to go
    on the lap is split into thirds.
    """
    if count < 2 * min_gates:
        return [Section(1, "Section 1", 1, count)]
    scores: dict[int, float] = dict.fromkeys(range(2, count + 1), 0.0)
    if geometry:
        by_k = {g.k: g for g in geometry}
        spacings = [g.spacing_m for g in geometry if g.spacing_m]
        median_spacing = statistics.median(spacings) if spacings else None
        for k in range(2, count + 1):
            turn = by_k.get(k - 1)
            if turn and turn.heading_change_deg is not None and turn.heading_change_deg >= 45:
                scores[k] += min(1.0, turn.heading_change_deg / 90.0)
            here = by_k.get(k)
            if here and here.spacing_m and median_spacing:
                ratio = here.spacing_m / median_spacing
                if ratio >= 1.8 or ratio <= 0.55:
                    scores[k] += 0.5
    if speeds:
        known = [v for v in speeds if v is not None]
        span = (max(known) - min(known)) if len(known) > 1 else 0.0
        if span > 1.0:
            for k in range(2, count + 1):
                a, b = speeds[k - 2], speeds[k - 1]
                if a is not None and b is not None and abs(b - a) / span >= 0.35:
                    scores[k] += 1.0
    bounds = [1, count + 1]
    stuck: set[tuple[int, int]] = set()
    while len(bounds) - 1 < max_sections:
        spans = [
            (bounds[i + 1] - bounds[i], bounds[i], bounds[i + 1])
            for i in range(len(bounds) - 1)
            if (bounds[i], bounds[i + 1]) not in stuck
        ]
        if not spans:
            break
        _, lo, hi = max(spans)
        candidates = [
            (scores[k], -abs(k - (lo + hi) / 2), k)
            for k in range(lo + min_gates, hi - min_gates + 1)
            if scores.get(k, 0.0) >= 0.9
        ]
        if not candidates:
            stuck.add((lo, hi))
            continue
        bounds = sorted([*bounds, max(candidates)[2]])
    if len(bounds) == 2:
        return equal_split(count, 3 if count >= 6 else 2)
    return [Section(i, f"Section {i}", bounds[i - 1], bounds[i] - 1) for i in range(1, len(bounds))]


# ── analysis ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSegments:
    race_id: int
    finished: bool
    laps: dict[int, list[Segment]]


def section_time(segments: Sequence[Segment], section: Section) -> int:
    return sum(s.gate_ms for k, s in enumerate(segments, start=1) if section.contains(k))


def _min_speed(segments: Sequence[Segment], section: Section) -> float | None:
    vals = [
        s.min_speed if s.min_speed is not None else s.avg_speed
        for k, s in enumerate(segments, start=1)
        if section.contains(k)
    ]
    known = [v for v in vals if v is not None]
    return min(known) if known else None


def _exit_speed(segments: Sequence[Segment], section: Section) -> float | None:
    for k, s in enumerate(segments, start=1):
        if k == section.last:
            return s.avg_speed
    return None


def _min_accel(segments: Sequence[Segment], section: Section) -> float | None:
    vals = [
        s.min_accel
        for k, s in enumerate(segments, start=1)
        if section.contains(k) and s.min_accel is not None
    ]
    return min(vals) if vals else None


@dataclass(frozen=True)
class SectionStats:
    section: Section
    instances: int  # complete laps measured
    best_ms: int | None
    median_ms: int | None
    worst_ms: int | None
    iqr_ms: int | None
    pb_ms: int | None  # the PB run's mean per-lap time in this section
    on_table_ms: int | None  # pb minus best-ever
    pb_min_speed: float | None
    best_min_speed: float | None  # in the best-ever instance
    pb_exit_speed: float | None
    pb_min_accel: float | None
    crashes: int
    verdict: str  # line | mistakes | solid | -
    trend: list[list[int]] = field(default_factory=list)  # [race_id, mean ms] per finished run
    trend_ms: int | None = None  # mean of last 3 runs minus the 3 before (negative = improving)

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["section"] = self.section.to_dict()
        return d


def _verdict(times: Sequence[int], pb: int | None) -> str:
    """``mistakes`` when a real share of laps blow up, ``line`` when the PB itself
    is well off the best-ever, else ``solid``."""
    if len(times) < 3:
        return "-"
    best, median = times[0], int(statistics.median(times))
    blown = sum(1 for t in times if t >= 1.5 * median) / len(times)
    if blown >= 0.15:
        return "mistakes"
    if pb is not None and pb - best >= max(150, 0.06 * best):
        return "line"
    return "solid"


def stats(
    sections: Sequence[Section],
    runs: Sequence[RunSegments],
    count: int,
    pb_race_id: int | None,
    crashes_by_segment: Mapping[int, int] | None = None,
) -> list[SectionStats]:
    """Per-section numbers over every complete lap of every finished run."""
    crashes_by_segment = crashes_by_segment or {}
    out: list[SectionStats] = []
    for section in sections:
        instances: list[tuple[int, int, float | None]] = []  # (race_id, ms, min_speed)
        pb_vals: list[tuple[int, float | None, float | None, float | None]] = []
        per_run: dict[int, list[int]] = {}
        for run in runs:
            if not run.finished:
                continue
            for segs in run.laps.values():
                if len(segs) != count:
                    continue
                ms = section_time(segs, section)
                instances.append((run.race_id, ms, _min_speed(segs, section)))
                per_run.setdefault(run.race_id, []).append(ms)
                if run.race_id == pb_race_id:
                    pb_vals.append(
                        (
                            ms,
                            _min_speed(segs, section),
                            _exit_speed(segs, section),
                            _min_accel(segs, section),
                        )
                    )
        crashes = sum(n for k, n in crashes_by_segment.items() if section.contains(k))
        if not instances:
            out.append(
                SectionStats(
                    section,
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    crashes,
                    "-",
                )
            )
            continue
        times = sorted(ms for _, ms, _ in instances)
        best = times[0]
        median = int(statistics.median(times))
        worst = times[-1]
        q = statistics.quantiles(times, n=4) if len(times) >= 4 else None
        iqr = int(q[2] - q[0]) if q else None
        pb = round(sum(v[0] for v in pb_vals) / len(pb_vals)) if pb_vals else None
        best_inst = min(instances, key=lambda v: v[1])
        trend = [[rid, round(sum(v) / len(v))] for rid, v in per_run.items()]
        trend_ms = None
        if len(trend) >= 6:
            recent = [v[1] for v in trend[-3:]]
            before = [v[1] for v in trend[-6:-3]]
            trend_ms = round(sum(recent) / 3 - sum(before) / 3)

        def _mean(
            idx: int, rows: list[tuple[int, float | None, float | None, float | None]] = pb_vals
        ) -> float | None:
            vals = [v[idx] for v in rows if v[idx] is not None]
            return round(sum(vals) / len(vals), 2) if vals else None  # type: ignore[arg-type]

        out.append(
            SectionStats(
                section=section,
                instances=len(times),
                best_ms=best,
                median_ms=median,
                worst_ms=worst,
                iqr_ms=iqr,
                pb_ms=pb,
                on_table_ms=(pb - best) if pb is not None else None,
                pb_min_speed=_mean(1),
                best_min_speed=best_inst[2],
                pb_exit_speed=_mean(2),
                pb_min_accel=_mean(3),
                crashes=crashes,
                verdict=_verdict(times, pb),
                trend=trend,
                trend_ms=trend_ms,
            )
        )
    return out
