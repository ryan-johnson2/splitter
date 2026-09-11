"""Page and API smoke tests over the real app with a temp DB and no game."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from splitter.app import create_app
from splitter.config import Config
from tests import helpers as h


@pytest.fixture
async def client(tmp_path: Any) -> AsyncIterator[AsyncClient]:
    app = create_app(Config(database_url=f"sqlite+aiosqlite:///{tmp_path}/web.db"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


async def test_pages_render_empty(client: AsyncClient) -> None:
    for path in ("/", "/races", "/tracks", "/settings", "/protocol", "/healthz"):
        r = await client.get(path)
        assert r.status_code == 200, path
    assert (await client.get("/healthz")).json()["ok"] is True


async def test_settings_roundtrip(client: AsyncClient) -> None:
    r = await client.post(
        "/settings",
        data={
            "game_host": "192.168.1.50",
            "game_port": "60003",
            "player_name": "Ryan",
            "auto_connect": "1",
            "telemetry_enabled": "1",
            "telemetry_store_hz": "10",
            "event_log_enabled": "1",
            "event_log_keep": "500",
            "brand_name": "Splitter",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    settings = client.app.state.settings  # type: ignore[attr-defined]
    assert settings.get("game_host") == "192.168.1.50"
    assert settings.get_int("telemetry_store_hz") == 10
    bridge = client.app.state.bridge  # type: ignore[attr-defined]
    assert bridge.host == "192.168.1.50" and bridge.enabled
    page = await client.get("/settings")
    assert "192.168.1.50" in page.text


async def test_manual_session_and_state(client: AsyncClient) -> None:
    r = await client.post(
        "/api/session", json={"track_name": "Loop", "quad_type": "Ape", "race_laps": 3}
    )
    assert r.status_code == 200 and r.json()["source"] == "manual"
    state = (await client.get("/api/state")).json()
    assert state["session"]["track_name"] == "Loop" and state["race"] is None
    assert (await client.post("/api/session", json={"track_name": " "})).status_code == 400


async def test_race_pages_after_a_run(client: AsyncClient) -> None:
    controller = client.app.state.controller  # type: ignore[attr-defined]
    await controller.handle_event(h.session())
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    for lap, gate, t, fin in h.two_lap_race():
        await controller.handle_event(h.racedata(lap, gate, t, fin))
    await controller.handle_event(h.status("race finished"))

    races = (await client.get("/api/races")).json()
    assert len(races) == 1 and races[0]["is_best"]
    detail = (await client.get("/api/races/1")).json()
    assert len(detail["gate_times"]) == 8 and len(detail["laps"]) == 2

    for path in (
        "/races",
        "/races/1",
        "/tracks",
        "/tracks/detail?track=Practice%20Loop&quad=Source%20One&laps=3",
        "/protocol",
        "/protocol?type=racedata",
    ):
        r = await client.get(path)
        assert r.status_code == 200, path
    assert "Practice Loop" in (await client.get("/races")).text
    assert "personal best" in (await client.get("/races/1")).text

    r = await client.post(
        "/races/1/edit",
        data={"track_name": "Renamed", "quad_type": "Source One", "race_laps": "3"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (await client.get("/api/races/1")).json()["track_name"] == "Renamed"

    r = await client.post("/races/1/delete", follow_redirects=False)
    assert r.status_code == 303
    assert (await client.get("/api/races/1")).status_code == 404
    assert (await client.get("/races/1")).status_code == 404


async def test_connection_endpoint_requires_host(client: AsyncClient) -> None:
    r = await client.post("/api/connection", json={"action": "connect"})
    assert r.status_code == 400
    r = await client.post("/api/connection", json={"action": "connect", "host": "10.0.0.5"})
    assert r.status_code == 200 and r.json()["host"] == "10.0.0.5"
    r = await client.post("/api/connection", json={"action": "disconnect"})
    assert r.status_code == 200
