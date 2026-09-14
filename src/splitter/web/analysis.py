"""Section analysis assembled from stored runs, for the track and race pages."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from splitter.core import geometry
from splitter.core import sections as sec
from splitter.core.crashes import Crash
from splitter.db import repos
from splitter.db.models import Race

GEOMETRY_RUNS = 8  # traces to average gate positions over (PB + most recent)


def gate_count(races: list[Race], best: Race | None) -> int:
    """Gates per lap for the group: the PB's, else the modal value of finished runs."""
    if best and best.gates_per_lap:
        return best.gates_per_lap
    counts = [r.gates_per_lap for r in races if r.status == "finished" and r.gates_per_lap]
    return max(set(counts), key=counts.count) if counts else 0


def complete_lap(race: Race, count: int) -> list[Any] | None:
    """The first complete lap of a run, as segments, or None."""
    for segs in sec.lap_segments(race.gate_times).values():
        if len(segs) == count:
            return segs
    return None


def crash_counter(races: list[Race]) -> Counter[int]:
    """Crashes per lap-relative segment across runs (0 = holeshot)."""
    counter: Counter[int] = Counter()
    for r in races:
        for c in repos.crashes_of(r):
            counter[c.segment] += 1
    return counter


@dataclass
class TrackAnalysis:
    count: int
    sections: list[sec.Section]
    stats: list[sec.SectionStats]
    labels: dict[int, str]
    gate_points: list[list[float]]  # [k, x, z] top-down, for the map
    holeshot_crashes: int
    crash_runs: int
    seeded: bool = False
    layout: list[dict[str, Any]] = field(default_factory=list)
    trends: list[dict[str, Any]] = field(default_factory=list)  # per section: name, best, points


async def track_analysis(
    db: AsyncSession, track_id: int, races: list[Race], best: Race | None
) -> TrackAnalysis | None:
    """Sections (seeding them from the data on first sight) and their stats."""
    count = gate_count(races, best)
    if not count or not track_id:
        return None
    finished = [r for r in races if r.status == "finished"]
    reference = best or (finished[-1] if finished else None)
    labels = sec.segment_labels(complete_lap(reference, count) or []) if reference else {}

    # Geometry from the traces of the PB and the most recent runs with telemetry.
    traced = [r for r in reversed(races) if r.telemetry_samples > 0]
    picked = ([best] if best and best.telemetry_samples else []) + [
        r for r in traced if not best or r.id != best.id
    ]
    runs_for_geometry = []
    for r in picked[:GEOMETRY_RUNS]:
        trace = await repos.telemetry_for_race(db, r.id)
        crossings = [
            (k, s.cumulative_ms)
            for segs in sec.lap_segments(r.gate_times).values()
            for k, s in enumerate(segs, start=1)
        ]
        runs_for_geometry.append((trace, crossings))
    positions = geometry.gate_positions(runs_for_geometry) if runs_for_geometry else {}
    geom = geometry.gate_geometry(positions, count) if positions else None

    sections = await repos.sections_for_track(db, track_id)
    seeded = False
    if not sections:
        speeds: list[float | None] | None = None
        lap = complete_lap(reference, count) if reference else None
        if lap:
            speeds = [s.avg_speed for s in lap]
        sections = sec.suggest(count, geom, speeds)
        await repos.save_sections(db, track_id, sections)
        seeded = True

    runs = [
        sec.RunSegments(r.id, r.status == "finished", sec.lap_segments(r.gate_times)) for r in races
    ]
    crashes = crash_counter(races)
    stats = sec.stats(sections, runs, count, best.id if best else None, crashes)
    return TrackAnalysis(
        count=count,
        sections=sections,
        stats=stats,
        labels=labels,
        gate_points=[[p.k, round(p.x, 2), round(p.z, 2)] for p in positions.values()],
        holeshot_crashes=crashes.get(0, 0),
        crash_runs=sum(1 for r in races if r.crash_count),
        seeded=seeded,
        layout=[s.to_dict() for s in sections],
        trends=[
            {
                "ordinal": st.section.ordinal,
                "name": st.section.name,
                "best": st.best_ms,
                "points": st.trend,  # [race_id, mean per-lap ms] per finished run
            }
            for st in stats
            if st.best_ms is not None
        ],
    )


@dataclass
class RaceSectionRow:
    section: sec.Section
    laps: list[dict[str, Any]]  # per lap: ms, ref_ms, delta, min_speed, crashes
    total_ms: int | None
    ref_total_ms: int | None
    delta_ms: int | None
    crashes: int


def race_sections(
    race: Race, best: Race | None, sections: list[sec.Section], count: int
) -> list[RaceSectionRow]:
    """This run per section and lap, against the same lap of the PB."""
    laps = sec.lap_segments(race.gate_times)
    ref_laps = sec.lap_segments(best.gate_times) if best and best.id != race.id else {}
    crashes: list[Crash] = repos.crashes_of(race)
    lap_numbers = sorted(laps)
    rows: list[RaceSectionRow] = []
    for section in sections:
        per_lap: list[dict[str, Any]] = []
        total = ref_total = 0
        have_all = have_ref = True
        for lap in lap_numbers:
            segs = laps[lap]
            full = len(segs) == count
            partial_ok = section.last <= len(segs)
            ms = sec.section_time(segs, section) if (full or partial_ok) else None
            ref = ref_laps.get(lap)
            ref_ms = sec.section_time(ref, section) if ref and len(ref) == count else None
            n_crash = sum(1 for c in crashes if c.lap == lap and section.contains(c.segment))
            speeds = [
                s.min_speed if s.min_speed is not None else s.avg_speed
                for k, s in enumerate(segs, start=1)
                if section.contains(k)
            ]
            known = [v for v in speeds if v is not None]
            per_lap.append(
                {
                    "lap": lap,
                    "ms": ms,
                    "ref_ms": ref_ms,
                    "delta": (ms - ref_ms) if ms is not None and ref_ms is not None else None,
                    "min_speed": min(known) if known else None,
                    "crashes": n_crash,
                }
            )
            if ms is None:
                have_all = False
            else:
                total += ms
            if ref_ms is None:
                have_ref = False
            else:
                ref_total += ref_ms
        complete = have_all and bool(per_lap)
        with_ref = complete and have_ref
        rows.append(
            RaceSectionRow(
                section=section,
                laps=per_lap,
                total_ms=total if complete else None,
                ref_total_ms=ref_total if with_ref else None,
                delta_ms=(total - ref_total) if with_ref else None,
                crashes=sum(1 for c in crashes if section.contains(c.segment)),
            )
        )
    return rows


def section_labels_for(race: Race, best: Race | None, count: int) -> dict[int, str]:
    """Gate labels from this run's first complete lap, else the PB's."""
    for r in (race, best):
        if r is not None and count:
            lap = complete_lap(r, count)
            if lap:
                return sec.segment_labels(lap)
    return {}
