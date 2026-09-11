"""Query helpers. Everything the web layer and controller need from the DB."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from splitter.core.splits import Reference, build_reference
from splitter.db.models import EventLog, GateTime, Lap, Race, TelemetrySample
from splitter.util import utcnow


def _finished_filter(track_name: str, quad_type: str, race_laps: int) -> Any:
    return (
        (Race.track_name == track_name)
        & (Race.quad_type == quad_type)
        & (Race.race_laps == race_laps)
        & (Race.status == "finished")
        & (Race.total_time_ms.is_not(None))
    )


async def get_best_race(
    session: AsyncSession, track_name: str, quad_type: str, race_laps: int
) -> Race | None:
    stmt = (
        select(Race)
        .where(_finished_filter(track_name, quad_type, race_laps))
        .order_by(Race.total_time_ms.asc(), Race.id.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def load_reference(
    session: AsyncSession, track_name: str, quad_type: str, race_laps: int
) -> Reference | None:
    best = await get_best_race(session, track_name, quad_type, race_laps)
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


async def recalculate_best(
    session: AsyncSession, track_name: str, quad_type: str, race_laps: int
) -> int | None:
    """Re-flag ``is_best`` for one track/quad/laps group; returns the best race id."""
    rows = (
        (
            await session.execute(
                select(Race).where(
                    (Race.track_name == track_name)
                    & (Race.quad_type == quad_type)
                    & (Race.race_laps == race_laps)
                )
            )
        )
        .scalars()
        .all()
    )
    finished = sorted(
        (r for r in rows if r.status == "finished" and r.total_time_ms is not None),
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
    status: str = ""
    limit: int = 100


async def list_races(session: AsyncSession, filters: RaceFilters) -> list[Race]:
    stmt = select(Race)
    if filters.track:
        stmt = stmt.where(Race.track_name == filters.track)
    if filters.quad:
        stmt = stmt.where(Race.quad_type == filters.quad)
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
    key = (race.track_name, race.quad_type, race.race_laps)
    await session.execute(delete(TelemetrySample).where(TelemetrySample.race_id == race_id))
    await session.delete(race)
    await session.commit()
    await recalculate_best(session, *key)
    return True


async def update_race(session: AsyncSession, race_id: int, **fields: Any) -> Race | None:
    race = await session.get(Race, race_id)
    if race is None:
        return None
    old_key = (race.track_name, race.quad_type, race.race_laps)
    for name, value in fields.items():
        setattr(race, name, value)
    await session.commit()
    new_key = (race.track_name, race.quad_type, race.race_laps)
    await recalculate_best(session, *old_key)
    if new_key != old_key:
        await recalculate_best(session, *new_key)
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


async def track_summaries(session: AsyncSession) -> list[TrackSummary]:
    """One row per (track, quad, laps) with counts and bests."""
    stmt = (
        select(
            Race.track_name,
            func.max(Race.scenery),
            Race.quad_type,
            Race.race_laps,
            func.count(Race.id),
            func.sum(func.iif(Race.status == "finished", 1, 0)),
            func.min(func.iif(Race.status == "finished", Race.total_time_ms, None)),
            func.max(Race.started_at),
        )
        .group_by(Race.track_name, Race.quad_type, Race.race_laps)
        .order_by(func.max(Race.started_at).desc())
    )
    out: list[TrackSummary] = []
    for track, scenery, quad, laps, runs, finished, best_ms, last in await session.execute(stmt):
        best_id: int | None = None
        best_lap: int | None = None
        if best_ms is not None:
            best_id = (
                await session.execute(
                    select(Race.id)
                    .where(_finished_filter(track, quad, laps) & (Race.total_time_ms == best_ms))
                    .order_by(Race.id)
                    .limit(1)
                )
            ).scalar()
            best_lap = (
                await session.execute(
                    select(func.min(Lap.lap_ms))
                    .join(Race, Race.id == Lap.race_id)
                    .where(_finished_filter(track, quad, laps))
                )
            ).scalar()
        out.append(
            TrackSummary(
                track_name=track,
                scenery=scenery or "",
                quad_type=quad,
                race_laps=laps,
                runs=runs,
                finished=finished or 0,
                best_ms=best_ms,
                best_race_id=best_id,
                best_lap_ms=best_lap,
                last_run_at=last,
            )
        )
    return out


async def races_for_track(
    session: AsyncSession, track_name: str, quad_type: str, race_laps: int
) -> list[Race]:
    stmt = (
        select(Race)
        .where(
            (Race.track_name == track_name)
            & (Race.quad_type == quad_type)
            & (Race.race_laps == race_laps)
        )
        .order_by(Race.id.asc())
    )
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
