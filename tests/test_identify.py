"""Sync phase 3, web side: the fingerprint registry and identification at ingest."""

from __future__ import annotations

import json

from sqlalchemy import select

from splitter.core import identify as identification
from splitter.core.fingerprint import Fingerprint
from splitter.core.geometry import GatePosition
from splitter.db import repos
from splitter.db.models import Race, TrackFingerprint
from splitter.game.controller import RaceController
from tests import helpers as h
from tests.sync_fixtures import Side

SQUARE = [(0.0, 1.0, 0.0), (20.0, 1.0, 0.0), (20.0, 1.0, 20.0), (0.0, 1.0, 20.0)]
GATES = 5
TOTAL_MS = (2 + 2 * GATES * 2) * 1000  # n_gate_race with two laps


def fp(offset: float = 0.0) -> Fingerprint:
    return Fingerprint(4, 1, tuple((x + offset, y, z) for x, y, z in SQUARE))


def known(track_id: int, offset: float = 0.0, name: str = "") -> identification.Known:
    return identification.known_from_fingerprint(
        track_id, 0, name or f"T{track_id}", "", "", fp(offset)
    )


async def fly(controller: RaceController, scale: float = 1.0, imu: bool = True) -> None:
    """A two-lap run on a five-gate track (identification needs four gates in
    common), flying along x at 20 m/s so every gate has its own position."""
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.racetype())
    await controller.handle_event(h.countdown(0))
    await controller.handle_event(h.ev("FinishGate", {"StartFinishGate": "True"}))
    ts = 1_000_000.0
    for lap, gate, t, fin in h.n_gate_race(GATES, 2, 0.0, scale):
        if imu:
            while ts - 1_000_000.0 < t * 1000:
                rel = (ts - 1_000_000.0) / 1000
                await controller.handle_event(h.imu(ts, 20 * rel, 0.0, 20.0, 0.0))
                ts += 1000 / 60
        await controller.handle_event(h.racedata(lap, gate, t, fin))
    await controller.handle_event(h.status("race finished"))


def test_identify_picks_a_clear_winner_and_flags_twins() -> None:
    reg = {1: known(1), 2: known(2, 100.0)}
    d = identification.identify(fp(1.0), reg)
    assert d.track is not None and d.track.track_id == 1 and not d.ambiguous
    assert [c[0].track_id for c in d.candidates] == [1]
    assert identification.identify(fp(50.0), reg).track is None
    # Twins: the same layout under two ids → the first in registry order, flagged.
    reg = {3: known(3, 0.5), 1: known(1)}
    d = identification.identify(fp(), reg)
    assert d.track is not None and d.track.track_id == 3 and d.ambiguous
    assert identification.identify(None, reg).track is None
    assert identification.identify(fp(), {}).track is None


async def test_ingest_learns_from_picked_runs_and_identifies_the_rest(
    web: Side, node: Side
) -> None:
    ctl, up = node.state.controller, node.state.uploader
    # Run 1: the node knows the track (session event) → the web learns the layout.
    await ctl.handle_event(h.session())
    await fly(ctl)
    await up.process_once()
    async with web.state.session_factory() as db:
        rows = (await db.execute(select(TrackFingerprint))).scalars().all()
        assert len(rows) == 1 and rows[0].track_id == 500 and rows[0].source == "learned"
        assert rows[0].gates_per_lap == GATES and rows[0].owner == node.state.settings.get(
            "node_id"
        )
        assert len(json.loads(rows[0].gates)) == GATES
    # The same layout again does not add a second row.
    await fly(ctl, scale=0.95)
    await up.process_once()
    async with web.state.session_factory() as db:
        assert len((await db.execute(select(TrackFingerprint))).scalars().all()) == 1

    # Run 3: the node has no track (cleared) → unidentified there, recognised here.
    await ctl.clear_session()
    await fly(ctl, scale=1.05)
    async with node.state.session_factory() as db:
        local = (await db.execute(select(Race).order_by(Race.id.desc()))).scalars().first()
        assert local is not None and local.track_id == 0 and local.reference_uuid == ""
    await up.process_once()
    async with web.state.session_factory() as db:
        got = await repos.get_race_by_uuid(db, local.uuid)
        assert got is not None
        assert got.track_id == 500 and got.session_source == "matched" and got.track_note == ""
        assert got.track_name == "Practice Loop"
        # Never measured at GO: a derived delta against the PB as of ingest.
        assert got.pb_delta_ms == round(TOTAL_MS * 1.05) - round(TOTAL_MS * 0.95)
        assert got.delta_source == "derived" and not got.is_best
    async with node.state.session_factory() as db:
        assert await repos.cache_get(db, repos.PBKey(500, 0, 3)) is not None
    assert "with no track yet" not in (await web.client.get("/races")).text


async def test_review_queue_bulk_edit_teaches_and_rerun_identifies(web: Side, node: Side) -> None:
    ctl, up = node.state.controller, node.state.uploader
    await ctl.clear_session()
    for scale in (1.0, 1.03):  # my synthetic path moves gates with time: keep it a few metres
        await fly(ctl, scale=scale)
    await up.process_once()
    async with web.state.session_factory() as db:
        assert await repos.unidentified_count(db) == 2
    assert "2 runs with no track yet" in (await web.client.get("/races")).text
    queue = (await web.client.get("/races?unidentified=1")).text
    assert 'id="review"' in queue and f"{GATES} gates" in queue and "Re-run identification" in queue
    r = await web.client.post("/races/identify", follow_redirects=False)
    assert "Nothing" in r.headers["location"]
    # Attribute one by bulk edit → registry learns → the other is identified on re-run.
    ids = [x["id"] for x in (await web.client.get("/api/races")).json()]
    r = await web.client.post(
        "/races/bulk",
        data={
            "race_ids": [str(ids[0])],
            "action": "update",
            "track_id": "777",
            "track_name": "Learned Track",
            "scene_id": "3",
            "track_source": "community",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    async with web.state.session_factory() as db:
        rows = (await db.execute(select(TrackFingerprint))).scalars().all()
        assert len(rows) == 1 and rows[0].track_id == 777 and rows[0].track_name == "Learned Track"
    r = await web.client.post("/races/identify", follow_redirects=False)
    assert "Identified" in r.headers["location"] and "1" in r.headers["location"]
    rows_api = (await web.client.get("/api/races")).json()
    assert {x["track_id"] for x in rows_api} == {777}
    matched = next(x for x in rows_api if x["session_source"] == "matched")
    assert matched["track_name"] == "Learned Track"
    async with web.state.session_factory() as db:
        assert await repos.unidentified_count(db) == 0
    # The next arrival with this layout is recognised straight away.
    await fly(ctl, scale=0.97)
    await up.process_once()
    rows_api = (await web.client.get("/api/races")).json()
    assert all(x["track_id"] == 777 for x in rows_api)
    assert sum(x["is_best"] for x in rows_api) == 1


async def test_twins_are_attributed_to_the_newest_and_flagged(web: Side, node: Side) -> None:
    # The web itself learns the layout under track 500 from its own run...
    await web.state.controller.handle_event(h.session())
    await fly(web.state.controller)
    async with web.state.session_factory() as db:
        (row,) = (await db.execute(select(TrackFingerprint))).scalars().all()
        # ...and the same layout is registered again under another track (a twin).
        db.add(
            TrackFingerprint(
                track_id=501,
                track_name="Sticky Track",
                gates_per_lap=row.gates_per_lap,
                gates=row.gates,
                source="learned",
                created_at=repos.utcnow(),
            )
        )
        await db.commit()
    ctl, up = node.state.controller, node.state.uploader
    await ctl.clear_session()
    await fly(ctl)
    await up.process_once()
    got = (await web.client.get("/api/races")).json()[0]
    assert got["track_id"] == 501 and got["track_name"] == "Sticky Track"
    assert "ambiguous" in (await web.client.get("/races")).text
    # No fingerprint (no IMU): stays unidentified, and the queue says why.
    await fly(ctl, imu=False)
    await up.process_once()
    got = (await web.client.get("/api/races")).json()[0]
    assert got["track_id"] == 0
    assert "no IMU" in (await web.client.get("/races?unidentified=1")).text


def test_known_positions_from_fingerprint() -> None:
    k = known(1)
    assert k.positions[1] == GatePosition(1, 0.0, 1.0, 0.0, 1) and k.gates_per_lap == 4
