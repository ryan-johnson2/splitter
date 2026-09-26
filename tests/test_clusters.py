"""Phase 4: clustering unidentified runs by layout and labelling a cluster."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from splitter.core.fingerprint import Fingerprint
from splitter.db import repos
from splitter.db.models import TrackFingerprint
from splitter.web import clusters
from tests.sync_fixtures import Side
from tests.test_identify import fly

SQUARE = [(0.0, 1.0, 0.0), (20.0, 1.0, 0.0), (20.0, 1.0, 20.0), (0.0, 1.0, 20.0)]


def member(i: int, offset: float, gates: int = 4, node: str = "n1") -> clusters.Member:
    pts = [(x + offset, y, z) for x, y, z in SQUARE][:gates] + [(50.0 + offset, 1.0, 50.0)] * max(
        0, gates - 4
    )
    return clusters.Member(
        i, datetime(2026, 1, 1) + timedelta(minutes=i), node, Fingerprint(gates, 1, tuple(pts))
    )


def test_cluster_groups_by_layout_and_gate_count() -> None:
    found = clusters.cluster(
        [member(1, 0), member(2, 1.5), member(3, 40.0), member(4, 0.5, node="n2"), member(5, 0, 5)]
    )
    assert [len(c.members) for c in found] == [3, 1, 1]
    first = found[0]
    assert first.nodes == {"n1", "n2"} and first.first_seen < first.last_seen
    cen = first.centroid
    assert cen.gates_per_lap == 4 and cen.positions[1][0] == (0 + 1.5 + 0.5) / 3
    assert found[1].members[0].race_id == 3 and found[2].gates_per_lap == 5
    # Too few located gates: left out.
    short = clusters.Member(
        9, datetime(2026, 1, 2), "n1", Fingerprint(4, 1, (SQUARE[0], None, None, None))
    )
    assert clusters.cluster([short]) == []


async def test_label_a_layout_attributes_the_cluster_and_future_runs(web: Side, node: Side) -> None:
    ctl, up = node.state.controller, node.state.uploader
    await ctl.clear_session()
    for scale in (1.0, 1.02):
        await fly(ctl, scale=scale)
    await up.process_once()
    page = (await web.client.get("/races/layouts")).text
    assert "Layout 1" in page and "2 runs" in page and 'name="race_ids"' in page
    assert "Layout 2" not in page and "gaming pc" in page
    ids = [x["id"] for x in (await web.client.get("/api/races")).json()]
    r = await web.client.post(
        "/races/layouts/label",
        data={
            "race_ids": [str(i) for i in ids],
            "action": "label",
            "track_id": "40001",
            "track_name": "USADT Champs Trial 01",
            "scene_id": "16",
            "track_source": "community",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Labelled" in r.headers["location"]
    rows = (await web.client.get("/api/races")).json()
    assert all(x["track_id"] == 40001 and x["session_source"] == "matched" for x in rows)
    assert sum(x["is_best"] for x in rows) == 1
    async with web.state.session_factory() as db:
        (row,) = (await db.execute(select(TrackFingerprint))).scalars().all()
        assert row.source == "labelled" and row.track_id == 40001 and row.owner == ""
    # The next run on the layout is recognised at ingest; the track page lists the layout.
    await fly(ctl, scale=0.98)
    await up.process_once()
    assert (await web.client.get("/api/races")).json()[0]["track_id"] == 40001
    track_page = (await web.client.get("/tracks/detail?track_id=40001&quad_model=0&laps=3")).text
    assert 'id="layouts"' in track_page and "labelled" in track_page
    r = await web.client.post(f"/tracks/40001/layouts/{row.id}/forget", follow_redirects=False)
    assert r.status_code == 303
    async with web.state.session_factory() as db:
        assert (await db.execute(select(TrackFingerprint))).scalars().all() == []
    assert (await web.client.get("/races/layouts")).status_code == 200


async def test_not_a_track_leaves_the_queue_for_good(web: Side, node: Side) -> None:
    ctl, up = node.state.controller, node.state.uploader
    await ctl.clear_session()
    await fly(ctl)
    await up.process_once()
    ids = [x["id"] for x in (await web.client.get("/api/races")).json()]
    r = await web.client.post(
        "/races/layouts/label",
        data={"race_ids": [str(ids[0])], "action": "ignore"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "not+a+track" in r.headers["location"]
    async with web.state.session_factory() as db:
        assert await repos.unidentified_count(db) == 0
        race = await repos.get_race(db, ids[0])
        assert race is not None and race.track_id == 0 and race.track_note == "not a track"
    # The same layout again: recognised as not a track on arrival, never queued.
    await fly(ctl, scale=1.01)
    await up.process_once()
    async with web.state.session_factory() as db:
        assert await repos.unidentified_count(db) == 0
    assert "Nothing to label" in (await web.client.get("/races/layouts")).text
    # Label without a pick is refused.
    r = await web.client.post(
        "/races/layouts/label",
        data={"race_ids": [str(ids[0])], "action": "label"},
        follow_redirects=False,
    )
    assert "Pick+the+track" in r.headers["location"]
