"""The bookend rule: trackless runs between two runs on the same track, from
the same node, are that track."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitter.db import repos
from splitter.db.models import Race, TrackFingerprint
from splitter.util import utcnow
from tests.sync_fixtures import Side, auth
from tests.test_controller import fly

T0 = datetime(2026, 9, 27, 20, 0, 0)


def race_row(
    at: datetime,
    track_id: int = 0,
    node: str = "n1",
    status: str = "aborted",
    fingerprint: str = "",
    **fields: Any,
) -> Race:
    return Race(
        uuid=uuid.uuid4().hex,
        node_id=node,
        track_id=track_id,
        track_name="Practice Loop" if track_id == 500 else ("Sticky Track" if track_id else ""),
        scenery="Beginner" if track_id == 500 else "",
        scene_id=29 if track_id == 500 else 0,
        track_source="community" if track_id else "",
        status=status,
        started_at=at,
        ended_at=at + timedelta(seconds=30),
        total_time_ms=30000 if status == "finished" else None,
        race_laps=3,
        fingerprint=fingerprint,
        **fields,
    )


async def add(sf: async_sessionmaker[AsyncSession], *rows: Race) -> list[int]:
    async with sf() as db:
        db.add_all(rows)
        await db.commit()
        return [r.id for r in rows]


async def tracks(sf: async_sessionmaker[AsyncSession], ids: list[int]) -> list[tuple[int, str]]:
    async with sf() as db:
        out = []
        for i in ids:
            r = await db.get(Race, i)
            assert r is not None
            out.append((r.track_id, r.session_source))
        return out


async def test_trackless_runs_between_two_on_the_same_track_get_it(session_factory: Any) -> None:
    m = timedelta(minutes=1)
    ids = await add(
        session_factory,
        race_row(T0, 500, status="finished"),
        race_row(T0 + 2 * m),
        race_row(T0 + 4 * m),
        race_row(T0 + 6 * m),
        race_row(T0 + 8 * m, 500, status="finished"),
    )
    async with session_factory() as db:
        closer = await db.get(Race, ids[-1])
        assert closer is not None
        got = await repos.infer_bookended(db, closer)
        assert sorted(r.id for r in got) == ids[1:4]
    assert await tracks(session_factory, ids[1:4]) == [(500, "bookend")] * 3
    async with session_factory() as db:
        r = await db.get(Race, ids[1])
        assert r is not None and r.track_name == "Practice Loop" and r.scene_id == 29


async def test_no_opener_or_a_different_track_leaves_them_alone(session_factory: Any) -> None:
    m = timedelta(minutes=1)
    # Nothing before the block: not inferred.
    ids = await add(session_factory, race_row(T0), race_row(T0 + m), race_row(T0 + 2 * m, 500))
    async with session_factory() as db:
        assert await repos.infer_bookended(db, (await db.get(Race, ids[-1]))) == []  # type: ignore[arg-type]
    assert await tracks(session_factory, ids[:2]) == [(0, "")] * 2
    # Opened by another track: not inferred.
    ids = await add(
        session_factory,
        race_row(T0 + 10 * m, 501),
        race_row(T0 + 11 * m),
        race_row(T0 + 12 * m, 500),
    )
    async with session_factory() as db:
        assert await repos.infer_bookended(db, (await db.get(Race, ids[-1]))) == []  # type: ignore[arg-type]
    assert await tracks(session_factory, ids[1:2]) == [(0, "")]
    # Another node's runs are not in the chain.
    ids = await add(
        session_factory,
        race_row(T0 + 20 * m, 500),
        race_row(T0 + 21 * m, node="n2"),
        race_row(T0 + 22 * m, 500),
    )
    async with session_factory() as db:
        assert await repos.infer_bookended(db, (await db.get(Race, ids[-1]))) == []  # type: ignore[arg-type]
    # A gap longer than one sitting breaks the chain.
    ids = await add(
        session_factory,
        race_row(T0 + 30 * m, 500),
        race_row(T0 + 31 * m),
        race_row(T0 + 31 * m + repos.BOOKEND_GAP + m, 500),
    )
    async with session_factory() as db:
        assert await repos.infer_bookended(db, (await db.get(Race, ids[-1]))) == []  # type: ignore[arg-type]


async def test_a_run_whose_gates_say_another_layout_is_skipped(session_factory: Any) -> None:
    m = timedelta(minutes=1)
    gates = [
        [0.0, 0.0, 0.0],
        [20.0, 0.0, 0.0],
        [40.0, 0.0, 0.0],
        [60.0, 0.0, 0.0],
        [80.0, 0.0, 0.0],
    ]
    far = [[g[0], g[1] + 500.0, g[2]] for g in gates]
    fp_same = json.dumps({"gates_per_lap": 5, "lap": 1, "gates": gates})
    fp_far = json.dumps({"gates_per_lap": 5, "lap": 1, "gates": far})
    async with session_factory() as db:
        db.add(
            TrackFingerprint(
                track_id=500,
                scene_id=29,
                track_name="Practice Loop",
                scenery="Beginner",
                track_source="community",
                gates_per_lap=5,
                gates=json.dumps(gates),
                source="labelled",
                owner="",
                origin_race_uuid="",
                created_at=utcnow(),
            )
        )
        await db.commit()
    ids = await add(
        session_factory,
        race_row(T0, 500, status="finished"),
        race_row(T0 + m, fingerprint=fp_far, gates_per_lap=5),  # clearly elsewhere
        race_row(T0 + 2 * m, fingerprint=fp_same, gates_per_lap=5),  # the same layout
        race_row(T0 + 3 * m),  # no trace at all
        race_row(T0 + 4 * m, 500, status="finished"),
    )
    async with session_factory() as db:
        got = await repos.infer_bookended(db, (await db.get(Race, ids[-1])))  # type: ignore[arg-type]
        assert sorted(r.id for r in got) == [ids[2], ids[3]]
    assert await tracks(session_factory, ids[1:4]) == [(0, ""), (500, "bookend"), (500, "bookend")]


async def test_the_sweep_and_the_web_apply_it(web: Side, node: Side) -> None:
    """A node pushes run A (track), three aborted runs (no track), run B (track):
    the web attributes the three as they are bookended."""
    from tests import helpers as h

    ctl = node.state.controller
    sf = web.state.session_factory
    m = timedelta(minutes=1)
    node_id = node.state.settings.get("node_id")
    # The whole story, hand-built on the web as if ingested in order.
    ids = await add(
        sf,
        race_row(T0, 500, node=node_id, status="finished"),
        race_row(T0 + m, node=node_id),
        race_row(T0 + 2 * m, node=node_id),
        race_row(T0 + 3 * m, node=node_id),
    )
    # The closing run arrives through ingest with its track.
    await ctl.handle_event(h.session())
    await fly(ctl, imu=True)
    async with node.state.session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        race.started_at = T0 + 4 * m
        await db.commit()
        doc = await repos.export_run(db, race, "test")
    r = await web.client.put(f"/api/ingest/runs/{race.uuid}", json=doc, headers=auth(web))
    assert r.status_code == 200 and r.json()["status"] == "created"
    assert await tracks(sf, ids[1:]) == [(500, "bookend")] * 3
    # The sweep is idempotent and reachable from the review queue's re-run.
    async with sf() as db:
        assert await repos.infer_bookended_all(db) == 0
    page = (await web.client.get("/races")).text
    assert "between" in page
