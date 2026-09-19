"""Query helpers. Everything the web layer and controller need from the DB."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from splitter.core.crashes import Crash
from splitter.core.geometry import GatePosition, gate_positions
from splitter.core.sections import Section, lap_segments
from splitter.core.splits import Reference, build_reference
from splitter.db.models import EventLog, GateTime, Lap, Race, TelemetrySample, TrackSection
from splitter.util import utcnow


@dataclass(frozen=True)
class PBKey:
    """What a personal best is kept for. ``track_id`` 0 (no online identity)
    means the race can never be a PB or a reference."""

    track_id: int
    quad_model_id: int
    race_laps: int

    @property
    def valid(self) -> bool:
        return self.track_id > 0


def race_key(race: Race) -> PBKey:
    return PBKey(race.track_id, race.quad_model_id, race.race_laps)


def _key_filter(key: PBKey) -> Any:
    return (
        (Race.track_id == key.track_id)
        & (Race.quad_model_id == key.quad_model_id)
        & (Race.race_laps == key.race_laps)
    )


def _finished_filter(key: PBKey) -> Any:
    # total_time_ms > 0: a zero total is never a real finish (see controller._finish_race).
    return _key_filter(key) & (Race.status == "finished") & (Race.total_time_ms > 0)


async def get_best_race(session: AsyncSession, key: PBKey) -> Race | None:
    if not key.valid:
        return None
    stmt = (
        select(Race)
        .where(_finished_filter(key))
        .order_by(Race.total_time_ms.asc(), Race.id.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def load_reference(session: AsyncSession, key: PBKey) -> Reference | None:
    best = await get_best_race(session, key)
    if best is None or best.total_time_ms is None:
        return None
    return reference_from_race(best)


def reference_from_race(race: Race) -> Reference:
    return build_reference(
        race_id=race.id,
        total_ms=race.total_time_ms or 0,
        crossings=[(g.seq, g.cumulative_ms, g.gate_ms) for g in race.gate_times],
        laps=[(lap.lap, lap.lap_ms) for lap in race.laps],
        gates_per_lap=race.gates_per_lap,
    )


async def recalculate_best(session: AsyncSession, key: PBKey) -> int | None:
    """Re-flag ``is_best`` for one track/quad/laps group; returns the best race id.

    Races without a track id are never best.
    """
    rows = (await session.execute(select(Race).where(_key_filter(key)))).scalars().all()
    best_id: int | None = None
    if key.valid:
        finished = sorted(
            (r for r in rows if r.status == "finished" and (r.total_time_ms or 0) > 0),
            key=lambda r: (r.total_time_ms or 0, r.id),
        )
        best_id = finished[0].id if finished else None
    for r in rows:
        r.is_best = r.id == best_id
    await session.commit()
    return best_id


@dataclass
class RaceFilters:
    track: str = ""
    quad: str = ""
    track_id: int = 0
    status: str = ""
    limit: int = 100


async def list_races(session: AsyncSession, filters: RaceFilters) -> list[Race]:
    stmt = select(Race)
    if filters.track:
        stmt = stmt.where(Race.track_name == filters.track)
    if filters.quad:
        stmt = stmt.where(Race.quad_type == filters.quad)
    if filters.track_id:
        stmt = stmt.where(Race.track_id == filters.track_id)
    if filters.status:
        stmt = stmt.where(Race.status == filters.status)
    stmt = stmt.order_by(Race.id.desc()).limit(filters.limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_race(session: AsyncSession, race_id: int) -> Race | None:
    return await session.get(Race, race_id)


async def delete_race(session: AsyncSession, race_id: int) -> bool:
    race = await session.get(Race, race_id)
    if race is None:
        return False
    key = race_key(race)
    await session.execute(delete(TelemetrySample).where(TelemetrySample.race_id == race_id))
    await session.delete(race)
    await session.commit()
    await recalculate_best(session, key)
    return True


async def update_race(session: AsyncSession, race_id: int, **fields: Any) -> Race | None:
    race = await session.get(Race, race_id)
    if race is None:
        return None
    old_key = race_key(race)
    for name, value in fields.items():
        setattr(race, name, value)
    await session.commit()
    new_key = race_key(race)
    await recalculate_best(session, old_key)
    if new_key != old_key:
        await recalculate_best(session, new_key)
    return race


async def distinct_tracks(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        select(Race.track_name).where(Race.track_name != "").distinct().order_by(Race.track_name)
    )
    return [r[0] for r in rows]


async def distinct_quads(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        select(Race.quad_type).where(Race.quad_type != "").distinct().order_by(Race.quad_type)
    )
    return [r[0] for r in rows]


@dataclass
class TrackSummary:
    track_id: int
    quad_model_id: int
    track_name: str
    scenery: str
    quad_type: str
    race_laps: int
    runs: int
    finished: int
    best_ms: int | None
    best_race_id: int | None
    best_lap_ms: int | None
    last_run_at: datetime | None

    @property
    def key(self) -> PBKey:
        return PBKey(self.track_id, self.quad_model_id, self.race_laps)


async def track_summaries(session: AsyncSession) -> list[TrackSummary]:
    """One row per PB key (track id, quad model, laps) with counts and bests.

    Runs without a track id are grouped under id 0 by name so they still show.
    """
    stmt = (
        select(
            Race.track_id,
            Race.quad_model_id,
            func.max(Race.track_name),
            func.max(Race.scenery),
            func.max(Race.quad_type),
            Race.race_laps,
            func.count(Race.id),
            func.sum(func.iif(Race.status == "finished", 1, 0)),
            func.min(func.iif(Race.status == "finished", Race.total_time_ms, None)),
            func.max(Race.started_at),
        )
        .group_by(
            Race.track_id,
            Race.quad_model_id,
            Race.race_laps,
            func.iif(Race.track_id == 0, Race.track_name, ""),
        )
        .order_by(func.max(Race.started_at).desc())
    )
    out: list[TrackSummary] = []
    for row in await session.execute(stmt):
        track_id, quad_model_id, track, scenery, quad, laps, runs, finished, best_ms, last = row
        key = PBKey(track_id, quad_model_id, laps)
        best_id: int | None = None
        best_lap: int | None = None
        if best_ms is not None and key.valid:
            best_id = (
                await session.execute(
                    select(Race.id)
                    .where(_finished_filter(key) & (Race.total_time_ms == best_ms))
                    .order_by(Race.id)
                    .limit(1)
                )
            ).scalar()
            best_lap = (
                await session.execute(
                    select(func.min(Lap.lap_ms))
                    .join(Race, Race.id == Lap.race_id)
                    .where(_finished_filter(key))
                )
            ).scalar()
        out.append(
            TrackSummary(
                track_id=track_id,
                quad_model_id=quad_model_id,
                track_name=track or "",
                scenery=scenery or "",
                quad_type=quad or "",
                race_laps=laps,
                runs=runs,
                finished=finished or 0,
                best_ms=best_ms if key.valid else None,
                best_race_id=best_id,
                best_lap_ms=best_lap,
                last_run_at=last,
            )
        )
    return out


async def races_for_key(session: AsyncSession, key: PBKey) -> list[Race]:
    stmt = select(Race).where(_key_filter(key)).order_by(Race.id.asc())
    return list((await session.execute(stmt)).scalars().all())


async def telemetry_for_race(session: AsyncSession, race_id: int) -> list[TelemetrySample]:
    stmt = (
        select(TelemetrySample)
        .where(TelemetrySample.race_id == race_id)
        .order_by(TelemetrySample.t_ms)
    )
    return list((await session.execute(stmt)).scalars().all())


async def add_event_log(
    session: AsyncSession, event_type: str, payload: Any, race_id: int | None
) -> None:
    session.add(
        EventLog(
            received_at=utcnow(),
            race_id=race_id,
            event_type=event_type,
            payload=json.dumps(payload, separators=(",", ":"))[:4000],
        )
    )


async def prune_event_log(session: AsyncSession, keep: int) -> int:
    """Drop everything but the newest ``keep`` rows; returns rows deleted."""
    cutoff = (
        await session.execute(
            select(EventLog.id).order_by(EventLog.id.desc()).offset(keep).limit(1)
        )
    ).scalar()
    if cutoff is None:
        return 0
    result = await session.execute(delete(EventLog).where(EventLog.id <= cutoff))
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def recent_events(
    session: AsyncSession, limit: int = 200, event_type: str = "", race_id: int | None = None
) -> list[EventLog]:
    stmt = select(EventLog)
    if event_type:
        stmt = stmt.where(EventLog.event_type == event_type)
    if race_id is not None:
        stmt = stmt.where(EventLog.race_id == race_id)
    stmt = stmt.order_by(EventLog.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def event_types(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        select(EventLog.event_type).distinct().order_by(EventLog.event_type)
    )
    return [r[0] for r in rows]


async def gate_time_count(session: AsyncSession, race_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count(GateTime.id)).where(GateTime.race_id == race_id)
            )
        ).scalar()
        or 0
    )


# ── sections / crashes ────────────────────────────────────────────


async def sections_for_track(session: AsyncSession, track_id: int) -> list[Section]:
    stmt = (
        select(TrackSection).where(TrackSection.track_id == track_id).order_by(TrackSection.ordinal)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [Section(r.ordinal, r.name, r.first_gate, r.last_gate) for r in rows]


async def save_sections(
    session: AsyncSession, track_id: int, sections: list[Section]
) -> list[Section]:
    """Replace the track's sections (already validated) in one go."""
    await session.execute(delete(TrackSection).where(TrackSection.track_id == track_id))
    now = utcnow()
    session.add_all(
        TrackSection(
            track_id=track_id,
            ordinal=s.ordinal,
            name=s.name,
            first_gate=s.first,
            last_gate=s.last,
            updated_at=now,
        )
        for s in sections
    )
    await session.commit()
    return sections


async def races_with_crashes(session: AsyncSession, track_id: int) -> list[Race]:
    """Every run on the track that recorded at least one crash (any status)."""
    stmt = (
        select(Race).where((Race.track_id == track_id) & (Race.crash_count > 0)).order_by(Race.id)
    )
    return list((await session.execute(stmt)).scalars().all())


def crashes_of(race: Race) -> list[Crash]:
    if not race.crashes:
        return []
    return [Crash.from_dict(d) for d in json.loads(race.crashes)]


async def races_with_telemetry(session: AsyncSession, race_ids: list[int]) -> list[int]:
    """Subset of ``race_ids`` that have a stored trace."""
    if not race_ids:
        return []
    stmt = (
        select(TelemetrySample.race_id)
        .where(TelemetrySample.race_id.in_(race_ids))
        .group_by(TelemetrySample.race_id)
    )
    return [int(r) for r in (await session.execute(stmt)).scalars().all()]


async def track_geometry(
    session: AsyncSession, track_id: int, limit: int = 6
) -> tuple[int | None, dict[int, GatePosition]]:
    """Gates per lap and averaged gate positions from recent finished traced runs."""
    stmt = (
        select(Race)
        .where(
            (Race.track_id == track_id) & (Race.status == "finished") & (Race.telemetry_samples > 0)
        )
        .order_by(Race.id.desc())
        .limit(limit)
    )
    races = list((await session.execute(stmt)).scalars().all())
    if not races:
        return None, {}
    counts = [r.gates_per_lap for r in races if r.gates_per_lap]
    count = max(set(counts), key=counts.count) if counts else None
    runs = []
    for r in races:
        trace = await telemetry_for_race(session, r.id)
        crossings = [
            (k, s.cumulative_ms)
            for segs in lap_segments(r.gate_times).values()
            for k, s in enumerate(segs, start=1)
        ]
        runs.append((trace, crossings))
    return count, gate_positions(runs)


@dataclass(frozen=True)
class KnownTrack:
    """A track with traced runs on file, named as its most recent run named it."""

    track_id: int
    track_name: str
    scenery: str
    scene_id: int
    track_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "track_name": self.track_name,
            "scenery": self.scenery,
            "scene_id": self.scene_id,
            "track_source": self.track_source,
        }


async def tracks_with_gate_count(
    session: AsyncSession, gates_per_lap: int, exclude_track_id: int = 0
) -> list[KnownTrack]:
    """Identified tracks that have a finished traced run with this many gates per lap.

    The cheap pre-filter before comparing geometry: a lap with N gates can only
    be a track whose laps have N gates.
    """
    stmt = (
        select(Race)
        .where(
            (Race.track_id > 0)
            & (Race.track_id != exclude_track_id)
            & (Race.status == "finished")
            & (Race.telemetry_samples > 0)
            & (Race.gates_per_lap == gates_per_lap)
        )
        .order_by(Race.id.desc())
    )
    out: dict[int, KnownTrack] = {}
    for r in (await session.execute(stmt)).scalars():
        if r.track_id not in out:
            out[r.track_id] = KnownTrack(
                r.track_id, r.track_name, r.scenery, r.scene_id, r.track_source
            )
    return list(out.values())
