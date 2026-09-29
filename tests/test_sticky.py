"""The sticky rule on the web: a run with no track carries the node's previous
track forward unless its own gate positions say it moved."""

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
M = timedelta(minutes=1)


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


async def carry(sf: async_sessionmaker[AsyncSession], race_id: int) -> list[int]:
    async with sf() as db:
        r = await db.get(Race, race_id)
        assert r is not None
        return sorted(x.id for x in await repos.infer_sticky(db, r))


async def test_trackless_runs_after_a_tracked_one_carry_it(session_factory: Any) -> None:
    ids = await add(
        session_factory,
        race_row(T0, 500, status="finished"),
        race_row(T0 + 2 * M),
        race_row(T0 + 4 * M),
        race_row(T0 + 6 * M),
    )
    # As each arrives: the first gets it from the opener, the rest through the chain.
    assert await carry(session_factory, ids[1]) == [ids[1]]
    assert await carry(session_factory, ids[3]) == [ids[2], ids[3]]
    assert await tracks(session_factory, ids[1:]) == [(500, "sticky")] * 3
    async with session_factory() as db:
        r = await db.get(Race, ids[1])
        assert r is not None and r.track_name == "Practice Loop" and r.scene_id == 29
        assert await repos.infer_sticky_all(db) == 0  # nothing left to do


async def test_nothing_carries_without_an_opener_across_nodes_or_a_long_gap(
    session_factory: Any,
) -> None:
    ids = await add(session_factory, race_row(T0), race_row(T0 + M))
    assert await carry(session_factory, ids[1]) == []
    ids = await add(session_factory, race_row(T0 + 10 * M, 500), race_row(T0 + 11 * M, node="n2"))
    assert await carry(session_factory, ids[1]) == []
    ids = await add(
        session_factory,
        race_row(T0 + 20 * M, 500),
        race_row(T0 + 21 * M + repos.STICKY_GAP),
    )
    assert await carry(session_factory, ids[1]) == []
    # A run labelled "not a track" ends the chain too.
    ids = await add(
        session_factory,
        race_row(T0 + 30 * M, 500),
        race_row(T0 + 31 * M, track_note="not a track"),
        race_row(T0 + 32 * M),
    )
    assert await carry(session_factory, ids[2]) == []
    assert await tracks(session_factory, ids[1:]) == [(0, ""), (0, "")]


GATES = [[0.0, 0.0, 0.0], [20.0, 0.0, 0.0], [40.0, 0.0, 0.0], [60.0, 0.0, 0.0], [80.0, 0.0, 0.0]]
FAR = [[g[0], g[1] + 500.0, g[2]] for g in GATES]
FP_SAME = json.dumps({"gates_per_lap": 5, "lap": 1, "gates": GATES})
FP_FAR = json.dumps({"gates_per_lap": 5, "lap": 1, "gates": FAR})
FP_OTHER_COUNT = json.dumps({"gates_per_lap": 4, "lap": 1, "gates": GATES[:4]})


async def know_practice_loop(sf: async_sessionmaker[AsyncSession]) -> None:
    async with sf() as db:
        db.add(
            TrackFingerprint(
                track_id=500,
                scene_id=29,
                track_name="Practice Loop",
                scenery="Beginner",
                track_source="community",
                gates_per_lap=5,
                gates=json.dumps(GATES),
                source="labelled",
                owner="",
                origin_race_uuid="",
                created_at=utcnow(),
            )
        )
        await db.commit()


async def test_a_run_whose_gates_say_it_moved_breaks_the_chain(session_factory: Any) -> None:
    await know_practice_loop(session_factory)
    ids = await add(
        session_factory,
        race_row(T0, 500, status="finished"),
        race_row(T0 + M, fingerprint=FP_SAME, gates_per_lap=5),  # the same layout: carries
        race_row(T0 + 2 * M),  # no trace: carries
        race_row(T0 + 3 * M, fingerprint=FP_FAR, gates_per_lap=5),  # clearly elsewhere: stops
        race_row(T0 + 4 * M),  # after the move: unknown
    )
    assert await carry(session_factory, ids[2]) == [ids[1], ids[2]]
    assert await carry(session_factory, ids[3]) == []
    assert await carry(session_factory, ids[4]) == []
    assert await tracks(session_factory, ids[1:]) == [
        (500, "sticky"),
        (500, "sticky"),
        (0, ""),
        (0, ""),
    ]
    # Known only with another gate count: that is a move too.
    ids = await add(
        session_factory,
        race_row(T0 + 10 * M, 500),
        race_row(T0 + 11 * M, fingerprint=FP_OTHER_COUNT, gates_per_lap=4),
    )
    assert await carry(session_factory, ids[1]) == []
    # The sweep agrees with the per-run passes.
    async with session_factory() as db:
        assert await repos.infer_sticky_all(db) == 0


async def test_the_sweep_walks_each_node_in_order(session_factory: Any) -> None:
    await know_practice_loop(session_factory)
    ids = await add(
        session_factory,
        race_row(T0, 500),
        race_row(T0 + M),
        race_row(T0 + 2 * M, fingerprint=FP_FAR, gates_per_lap=5),
        race_row(T0 + 3 * M),
        race_row(T0 + 4 * M, 501),
        race_row(T0 + 5 * M),
        race_row(T0 + 1 * M, node="n2"),  # no opener on its node
    )
    async with session_factory() as db:
        assert await repos.infer_sticky_all(db) == 2
    assert await tracks(session_factory, ids[1:]) == [
        (500, "sticky"),
        (0, ""),
        (0, ""),
        (501, ""),
        (501, "sticky"),
        (0, ""),
    ]


async def test_the_web_applies_it_at_ingest(web: Side, node: Side) -> None:
    """The node pushes a run it could not name; the web carries its previous track."""
    ctl = node.state.controller
    sf = web.state.session_factory
    node_id = node.state.settings.get("node_id")
    ids = await add(sf, race_row(T0, 500, node=node_id, status="finished"))
    await fly(ctl)  # no session set on the node: a run with no track
    async with node.state.session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.track_id == 0
        race.started_at = T0 + M
        await db.commit()
        doc = await repos.export_run(db, race, "test")
    r = await web.client.put(f"/api/ingest/runs/{race.uuid}", json=doc, headers=auth(web))
    assert r.status_code == 200 and r.json()["status"] == "created"
    async with sf() as db:
        got = await repos.get_race_by_uuid(db, race.uuid)
        assert got is not None and got.track_id == 500 and got.session_source == "sticky"
        assert got.id != ids[0]
    page = (await web.client.get("/races")).text
    assert "carried" in page


# ── the same rule for the quad ─────────────────────────────────────────


async def quads(sf: async_sessionmaker[AsyncSession], ids: list[int]) -> list[tuple[int, str]]:
    async with sf() as db:
        out = []
        for i in ids:
            r = await db.get(Race, i)
            assert r is not None
            out.append((r.quad_model_id, r.quad_source))
        return out


async def test_quadless_runs_carry_the_previous_quad_whatever_the_track(
    session_factory: Any,
) -> None:
    ids = await add(
        session_factory,
        race_row(T0, 500, status="finished", quad_type="LightSwitch", quad_model_id=108),
        race_row(T0 + 2 * M, 500, status="finished"),  # tracked, no quad
        race_row(T0 + 4 * M, 501, status="finished"),  # another track: the quad chain goes on
        race_row(T0 + 6 * M),  # no track either
        race_row(T0 + 5 * 60 * M, 500, status="finished"),  # next day: nothing to carry from
    )
    async with session_factory() as db:
        r = await db.get(Race, ids[3])
        assert r is not None
        assert sorted(x.id for x in await repos.infer_sticky_quad(db, r)) == ids[1:4]
        assert await repos.infer_sticky_quad(db, r) == []  # has one now
    assert await quads(session_factory, ids) == [
        (108, ""),
        (108, "sticky"),
        (108, "sticky"),
        (108, "sticky"),
        (0, ""),
    ]
    async with session_factory() as db:
        r = await db.get(Race, ids[1])
        assert r is not None and r.quad_type == "LightSwitch"
        # Runs moved from the unknown-quad key to the LightSwitch key: one PB there.
        best = await repos.get_best_race(db, repos.PBKey(500, 108, 3))
        assert best is not None and best.id == ids[0]
        left = await repos.get_best_race(db, repos.PBKey(500, 0, 3))
        assert left is not None and left.id == ids[4]  # only the next-day run is still there
        assert await repos.infer_sticky_quads_all(db) == 0


async def test_the_quad_sweep_and_a_named_but_uncatalogued_quad(session_factory: Any) -> None:
    ids = await add(
        session_factory,
        race_row(T0, 500, quad_type="Homebuilt 5"),  # a name the catalog lacks still counts
        race_row(T0 + M, 500),
        race_row(T0 + 2 * M, 500, node="n2"),  # another node: nothing to carry
        race_row(T0 + 3 * M, 500, node="n2", quad_model_id=89, quad_type="AOS 5.5"),
        race_row(T0 + 4 * M, 500, node="n2"),
    )
    async with session_factory() as db:
        assert await repos.infer_sticky_quads_all(db) == 2
    got = await quads(session_factory, ids)
    assert got == [(0, ""), (0, "sticky"), (0, ""), (89, ""), (89, "sticky")]
    async with session_factory() as db:
        r = await db.get(Race, ids[1])
        assert r is not None and r.quad_type == "Homebuilt 5"


async def test_the_web_carries_the_quad_at_ingest(web: Side, node: Side) -> None:
    ctl = node.state.controller
    sf = web.state.session_factory
    node_id = node.state.settings.get("node_id")
    await add(
        sf,
        race_row(
            T0, 500, node=node_id, status="finished", quad_type="LightSwitch", quad_model_id=108
        ),
    )
    await fly(ctl)  # no session on the node: no track, no quad
    async with node.state.session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.quad_model_id == 0 and race.quad_type == ""
        race.started_at = T0 + M
        await db.commit()
        doc = await repos.export_run(db, race, "test")
    r = await web.client.put(f"/api/ingest/runs/{race.uuid}", json=doc, headers=auth(web))
    assert r.status_code == 200
    async with sf() as db:
        got = await repos.get_race_by_uuid(db, race.uuid)
        assert got is not None and got.track_id == 500 and got.session_source == "sticky"
        assert got.quad_model_id == 108 and got.quad_source == "sticky"
    assert "quad carried" in (await web.client.get("/races")).text
