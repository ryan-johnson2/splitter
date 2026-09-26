"""Sync phase 2: a node pushes runs to a web in the same process (ASGI transport,
no network); the web acknowledges with the reference bundle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from splitter.app import create_app
from splitter.config import Config
from splitter.db import repos
from splitter.db.models import Outbox, Race, RunTombstone
from splitter.sync.uploader import token_allowed
from splitter.util import utcnow
from tests import helpers as h
from tests.test_controller import fly
from tests.test_web import _FakeCatalog

WEB_URL = "http://splitter.local:8100"


class Side:
    def __init__(self, app: Any, client: AsyncClient) -> None:
        self.app, self.client = app, client

    @property
    def state(self) -> Any:
        return self.app.state


@pytest.fixture
async def web(tmp_path: Any) -> AsyncIterator[Side]:
    app = create_app(Config(database_url=f"sqlite+aiosqlite:///{tmp_path}/web.db"))
    app.state.catalog = _FakeCatalog()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=WEB_URL) as c,
    ):
        r = await c.post(
            "/settings/ingest-token", data={"action": "generate"}, follow_redirects=False
        )
        assert r.status_code == 303
        yield Side(app, c)


@pytest.fixture
async def node(tmp_path: Any, web: Side) -> AsyncIterator[Side]:
    app = create_app(Config(database_url=f"sqlite+aiosqlite:///{tmp_path}/node.db"))
    app.state.catalog = _FakeCatalog()
    app.state.sync_client = lambda url: AsyncClient(
        transport=ASGITransport(app=web.app), base_url=url
    )
    app.state.sync_autorun = False
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://node") as c,
    ):
        await configure(c, url=WEB_URL, token=web.state.settings.get("ingest_token"))
        yield Side(app, c)


async def configure(c: AsyncClient, url: str, token: str, keep: str = "1") -> None:
    r = await c.post(
        "/settings",
        data={
            "upstream_url": url,
            "upstream_token": token,
            "keep_local_runs": keep,
            "node_name": "gaming pc",
            "telemetry_enabled": "1",
            "telemetry_store_hz": "20",
            "event_log_keep": "500",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303


def auth(web: Side) -> dict[str, str]:
    return {"Authorization": f"Bearer {web.state.settings.get('ingest_token')}"}


async def test_a_run_is_pushed_and_acknowledged(web: Side, node: Side) -> None:
    ctl, up = node.state.controller, node.state.uploader
    assert up.configured and not up.token_blocked
    await ctl.handle_event(h.session())
    await fly(ctl, imu=True)
    assert up.pending == 1 and ctl.snapshot()["sync"]["pending"] == 1
    async with node.state.session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.seq == 1
    assert node.state.settings.get_int("node_seq") == 1

    await up.process_once()
    assert up.pending == 0 and up.last_error == "" and up.last_ack_at
    async with web.state.session_factory() as db:
        got = (await db.execute(select(Race))).scalar_one()
        assert got.uuid == race.uuid and got.origin == "ingest" and got.received_at
        assert got.node_id == node.state.settings.get("node_id") and got.seq == 1
        assert got.is_best and got.telemetry_samples > 0 and got.fingerprint
        (ns,) = await repos.nodes_status(db)
        assert ns.node.name == "gaming pc" and ns.runs == 1 and ns.pending == 0
        assert ns.node.imu_seen_at is not None
    assert "Receiving from" in (await web.client.get("/races")).text
    async with node.state.session_factory() as db:
        row = await db.get(Outbox, race.uuid)
        assert row is not None and row.acked_at and row.attempts == 0
        assert await db.get(Race, race.id) is not None  # keep_local_runs on
        cached = await repos.cache_get(db, repos.race_key(race))
        assert cached and cached["reference"]["race_uuid"] == race.uuid
        assert cached["geometry"]["gates_per_lap"] == 3
    # Local and remote say the same PB; the local row wins the tie.
    assert ctl.reference is not None and not ctl.reference.remote
    # The same document again is a no-op on the web.
    doc = (await node.client.get(f"/api/races/{race.id}/export")).json()
    r = await web.client.put(f"/api/ingest/runs/{race.uuid}", json=doc, headers=auth(web))
    assert r.status_code == 200 and r.json()["status"] == "exists"
    assert r.json()["reference"]["key"]["track_id"] == 500


async def test_failures_back_off_and_refusals_are_terminal(web: Side, node: Side) -> None:
    ctl, up = node.state.controller, node.state.uploader
    await configure(node.client, url=WEB_URL, token="wrong")
    await ctl.handle_event(h.session())
    await fly(ctl)
    await up.process_once()
    assert up.pending == 1 and "401" in up.last_error
    async with node.state.session_factory() as db:
        row = (await db.execute(select(Outbox))).scalar_one()
        assert row.attempts == 1 and row.next_at > utcnow() + timedelta(seconds=3)
        uuid = row.race_uuid
    # Fixing the token retries at once.
    await configure(node.client, url=WEB_URL, token=web.state.settings.get("ingest_token"))
    await up.process_once()
    assert up.pending == 0 and up.last_error == ""

    # Deleted on the web: a re-send is refused for good, never retried.
    web_id = (await web.client.get("/api/races")).json()[0]["id"]
    assert (await web.client.delete(f"/api/races/{web_id}")).status_code == 200
    async with node.state.session_factory() as db:
        row = await db.get(Outbox, uuid)
        assert row is not None
        row.acked_at = None
        await db.commit()
    await up.process_once()
    assert up.terminal == 1 and "deleted" in up.last_error
    async with node.state.session_factory() as db:
        row = await db.get(Outbox, uuid)
        assert row is not None and row.terminal == "deleted"
    assert (await node.client.get("/api/sync")).json()["outbox"][0]["terminal"] == "deleted"
    # Retry puts it back in the queue (and it is refused again).
    assert (await node.client.post("/api/sync/retry")).json()["requeued"] == 1
    assert up.terminal == 0 and up.pending == 1

    # A newer document version and a malformed one.
    doc = (await node.client.get("/api/races/1/export")).json()
    r = await web.client.put(
        f"/api/ingest/runs/{doc['uuid']}", json=doc | {"doc_version": 99}, headers=auth(web)
    )
    assert r.status_code == 409 and r.json()["supported"] == 1
    r = await web.client.put(
        "/api/ingest/runs/" + "c" * 32, json={"doc_version": 1, "uuid": "c" * 32}, headers=auth(web)
    )
    assert r.status_code == 422
    r = await web.client.put("/api/ingest/runs/" + "d" * 32, json=doc, headers=auth(web))
    assert r.status_code == 400  # uuid mismatch


async def test_keep_local_off_deletes_after_ack_without_a_tombstone(web: Side, node: Side) -> None:
    ctl, up = node.state.controller, node.state.uploader
    await configure(
        node.client, url=WEB_URL, token=web.state.settings.get("ingest_token"), keep="0"
    )
    await ctl.handle_event(h.session())
    await fly(ctl, imu=True)
    await up.process_once()
    async with node.state.session_factory() as db:
        assert (await db.execute(select(Race))).scalars().all() == []
        assert (await db.execute(select(RunTombstone))).scalars().all() == []
    assert len((await web.client.get("/api/races")).json()) == 1
    # The reference for the next run now comes from the web.
    assert ctl.reference is not None and ctl.reference.remote
    assert ctl.reference.total_ms == 14000 and ctl.snapshot()["reference"]["remote"]
    # ...and the track check has geometry from the bundle, not from local runs.
    assert await ctl._track_geometry(500) is not None
    # Races/Tracks leave the nav; the Web link stays.
    html = (await node.client.get("/")).text
    assert 'href="/races"' not in html and "Web ↗" in html and 'id="web-dot"' in html
    # A second run is measured against the remote PB and records its uuid.
    await fly(ctl, scale=0.9)
    async with node.state.session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.reference_race_id is None and race.reference_uuid
        assert race.pb_delta_ms == -1400


async def test_seq_has_no_gap_after_a_zero_crossing_abort(web: Side, node: Side) -> None:
    ctl = node.state.controller
    await ctl.handle_event(h.session())
    await fly(ctl)
    await ctl.handle_event(h.status("start"))
    await ctl.handle_event(h.countdown(0))
    await ctl.handle_event(h.status("abort"))
    await fly(ctl)
    async with node.state.session_factory() as db:
        seqs = sorted(r.seq for r in (await db.execute(select(Race))).scalars())
        assert seqs == [1, 2]


async def test_cold_start_reference_comes_from_the_web(web: Side, node: Side) -> None:
    # The web already holds a run for the key; the node has nothing.
    await web.state.controller.handle_event(h.session())
    await fly(web.state.controller, imu=True)
    ctl, up = node.state.controller, node.state.uploader
    q = node.state.hub.subscribe()
    await ctl.handle_event(h.session())
    assert ctl.reference is None  # not yet: the request is queued
    await up.process_once()
    assert ctl.reference is not None and ctl.reference.remote and ctl.reference.total_ms == 14000
    assert any(m["type"] == "reference" for m in h.drain(q))
    # Purge with nothing acked is a no-op; then push one and purge it.
    assert (await node.client.post("/api/sync/purge")).json()["deleted"] == 0
    await fly(ctl, scale=1.1)
    await up.process_once()
    assert (await node.client.post("/api/sync/purge")).json()["deleted"] == 1
    async with node.state.session_factory() as db:
        assert (await db.execute(select(Race))).scalars().all() == []


async def test_ping_and_the_token_guard(web: Side, node: Side) -> None:
    r = await node.client.post("/api/sync/test")
    assert r.json()["ok"] and r.json()["doc_version"] == 1
    assert token_allowed("https://splitter.example.net")
    assert token_allowed("http://192.168.1.10:8100") and token_allowed("http://splitter.local")
    assert token_allowed("http://127.0.0.1:8100") and token_allowed("http://lxc-host")
    assert not token_allowed("http://8.8.8.8") and not token_allowed("http://example.com")
    await configure(node.client, url="http://example.com", token="t")
    up = node.state.uploader
    assert up.token_blocked and not (await node.client.post("/api/sync/test")).json()["ok"]
    assert "blocked" in (await node.client.get("/settings")).text


async def test_ingest_requires_the_token(web: Side, node: Side) -> None:
    r = await web.client.get("/api/ingest/ping")
    assert r.status_code == 401
    r = await web.client.get("/api/ingest/ping", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await web.client.get("/api/reference", params={"track_id": 500}, headers=auth(web))
    assert r.status_code == 200 and r.json()["reference"] is None
    await web.client.post("/settings/ingest-token", data={"action": "disable"})
    assert (await web.client.get("/api/ingest/ping", headers=auth(web))).status_code == 403
    # No upstream on the web itself: no Web indicator, Races stays in the nav.
    html = (await web.client.get("/")).text
    assert 'id="web-dot"' not in html and 'href="/races"' in html


async def test_earlier_and_imported_runs_are_queued_too(web: Side, node: Side) -> None:
    ctl, up = node.state.controller, node.state.uploader
    # Recorded with no upstream: queued anyway (seq 1), pushed once one is set.
    await configure(node.client, url="", token="")
    assert not up.configured
    await ctl.handle_event(h.session())
    await fly(ctl)
    # A run from before the outbox existed: no row at all.
    async with node.state.session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        await db.delete(await db.get(Outbox, race.uuid))
        race.seq = 0
        await db.commit()
    # And a hand-imported one.
    doc = (await node.client.get(f"/api/races/{race.id}/export")).json()
    doc = doc | {"uuid": "e" * 32, "race": dict(doc["race"], total_time_ms=15000)}
    assert (await node.client.post("/api/import", json=doc)).json()["created"] == 1
    await configure(node.client, url=WEB_URL, token=web.state.settings.get("ingest_token"))
    assert up.pending == 2
    await up.process_once()
    assert up.pending == 0
    got = (await web.client.get("/api/races")).json()
    assert len(got) == 2
    # The imported run had no node: the importing install became its node.
    assert {r["node_id"] for r in got} == {node.state.settings.get("node_id")}
