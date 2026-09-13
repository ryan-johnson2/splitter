"""Page and API smoke tests over the real app with a temp DB and no game."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from splitter.app import create_app
from splitter.config import Config
from splitter.game.catalog import SearchResult, TrackRef
from tests import helpers as h
from tests.conftest import fake_resolve


class _FakeCatalog:
    """Stands in for TrackCatalog: no network, canned results, records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, q: str, source: str = "", limit: int = 30) -> SearchResult:
        self.calls.append((q, source, limit))
        res = SearchResult()
        if q:
            res.tracks = [
                TrackRef("community", 40001, 16, "USADT Champs Trial 01", "Intermediate", "x")
            ]
        return res

    async def resolve(self, name: str) -> TrackRef | None:
        return await fake_resolve(name)


@pytest.fixture
async def client(tmp_path: Any) -> AsyncIterator[AsyncClient]:
    app = create_app(Config(database_url=f"sqlite+aiosqlite:///{tmp_path}/web.db"))
    app.state.catalog = _FakeCatalog()
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
    # A typed name alone is refused: PBs need the online track id.
    r = await client.post(
        "/api/session", json={"track_name": "Loop", "quad_type": "Ape", "race_laps": 3}
    )
    assert r.status_code == 400 and "online id" in r.json()["detail"]
    r = await client.post(
        "/api/session",
        json={"track_name": "Loop", "track_id": 77, "quad_type": "Ape", "race_laps": 3},
    )
    assert r.status_code == 200 and r.json()["source"] == "manual"
    assert r.json()["identified"] is True
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
        "/tracks/detail?track_id=500&quad_model=0&laps=3",
        "/protocol",
        "/protocol?type=racedata",
    ):
        r = await client.get(path)
        assert r.status_code == 200, path
    assert "Practice Loop" in (await client.get("/races")).text
    assert "personal best" in (await client.get("/races/1")).text
    assert races[0]["track_id"] == 500 and races[0]["track_source"] == "community"
    tracks_page = (await client.get("/tracks")).text
    assert "track_id=500" in tracks_page and "#500" in tracks_page

    # Edit: names alone keep the ids; a pick (> 0) re-attributes and re-flags PBs.
    r = await client.post(
        "/races/1/edit",
        data={"track_name": "Renamed", "quad_type": "Source One", "race_laps": "3"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    d = (await client.get("/api/races/1")).json()
    assert d["track_name"] == "Renamed" and d["track_id"] == 500 and d["is_best"]
    r = await client.post(
        "/races/1/edit",
        data={
            "track_name": "Renamed",
            "race_laps": "3",
            "track_id": "900",
            "scene_id": "8",
            "track_source": "official",
            "quad_model_id": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    d = (await client.get("/api/races/1")).json()
    assert d["track_id"] == 900 and d["scene_id"] == 8 and d["track_source"] == "official"
    assert d["quad_model_id"] == 1 and d["quad_class_id"] == 1 and d["quad_type"] == "Gravity 250"
    assert d["is_best"]  # still the only finished run in its (new) group

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


async def test_track_search_endpoint(client: AsyncClient) -> None:
    fake = _FakeCatalog()
    client.app.state.catalog = fake  # type: ignore[attr-defined]
    r = await client.get("/api/tracks/search", params={"q": "usadt", "source": "community"})
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == {}
    assert body["tracks"][0] == {
        "source": "community",
        "track_id": 40001,
        "scene_id": 16,
        "name": "USADT Champs Trial 01",
        "kind": "Intermediate",
        "author": "x",
        "scenery": "Empty Scene Day",
    }
    assert fake.calls == [("usadt", "community", 30)]
    r = await client.get("/api/tracks/search", params={"q": "", "limit": "500"})
    assert r.json() == {"tracks": [], "errors": {}, "available": True}
    assert fake.calls[-1] == ("", "", 100)


async def test_session_with_picked_track_persists_ids(client: AsyncClient) -> None:
    r = await client.post(
        "/api/session",
        json={
            "track_name": "USADT Champs Trial 01",
            "scenery": "Empty Scene Day",
            "track_id": 40001,
            "scene_id": 16,
            "track_source": "community",
            "race_laps": 3,
        },
    )
    assert r.status_code == 200
    s = r.json()
    assert s["track_id"] == 40001 and s["scene_id"] == 16 and s["track_source"] == "community"
    assert s["source"] == "manual" and s["known"] is True
    settings = client.app.state.settings  # type: ignore[attr-defined]
    assert (
        settings.get("last_track_id") == "40001"
        and settings.get("last_track_source") == "community"
    )
    # An unknown source is ignored; a hand-typed name without an id is refused.
    r = await client.post(
        "/api/session", json={"track_name": "Typed", "track_id": 5, "track_source": "bogus"}
    )
    assert r.json()["track_source"] == ""
    r = await client.post("/api/session", json={"track_name": "Typed"})
    assert r.status_code == 400


async def test_quads_endpoint_and_session_quad(client: AsyncClient) -> None:
    body = (await client.get("/api/quads")).json()
    assert body["classes"][0]["class_name"] == "Racing"
    gemfan = next(
        m for c in body["classes"] for m in c["models"] if m["name"] == "Armattan Chameleon"
    )
    r = await client.post(
        "/api/session",
        json={"track_name": "T", "track_id": 1, "quad_model_id": gemfan["model_id"]},
    )
    s = r.json()
    assert s["quad_type"] == "Armattan Chameleon" and s["quad_class_id"] == 1
    settings = client.app.state.settings  # type: ignore[attr-defined]
    assert settings.get("last_quad_model_id") == str(gemfan["model_id"])


async def test_unresolved_game_session_never_sets_a_pb(client: AsyncClient) -> None:
    controller = client.app.state.controller  # type: ignore[attr-defined]
    await controller.handle_event(h.session(track="Mystery Track"))
    assert controller.session.track_id == 0 and controller.session.identified is False
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    for lap, gate, t, fin in h.two_lap_race():
        await controller.handle_event(h.racedata(lap, gate, t, fin))
    await controller.handle_event(h.status("race finished"))
    races = (await client.get("/api/races")).json()
    assert races[0]["status"] == "finished" and races[0]["is_best"] is False
    assert "no id" in (await client.get("/races")).text
    r = await client.get("/tracks/detail?track_id=0&quad_model=0&laps=3&track=Mystery%20Track")
    assert r.status_code == 200 and "no online id" in r.text


async def test_time_format_setting(client: AsyncClient) -> None:
    from splitter.core.timeparse import format_ms

    assert format_ms(144359) == "144.359" and format_ms(144359, "mmss") == "2:24.359"
    assert format_ms(144359, "both") == "2:24.359 (144.359)"
    assert format_ms(47005, "both") == "47.005" and format_ms(None) == "--"
    page = (await client.get("/settings")).text
    assert 'name="time_format"' in page and 'window.SPLITTER_TIME_FORMAT = "seconds"' in page
    r = await client.post(
        "/settings",
        data={
            "game_host": "",
            "game_port": "60003",
            "brand_name": "Splitter",
            "time_format": "mmss",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert 'window.SPLITTER_TIME_FORMAT = "mmss"' in (await client.get("/")).text


async def test_service_worker_and_manifest(client: AsyncClient) -> None:
    r = await client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "splitter-" in r.text
    m = (await client.get("/static/manifest.webmanifest")).json()
    assert m["display"] == "fullscreen" and any(i["sizes"] == "512x512" for i in m["icons"])
    assert "/sw.js?v=" in (await client.get("/")).text


async def test_bulk_edit_flash_names_the_runs(client: AsyncClient) -> None:
    controller = client.app.state.controller  # type: ignore[attr-defined]
    for _ in range(2):
        await controller.handle_event(h.session())
        await controller.handle_event(h.status("start"))
        await controller.handle_event(h.countdown(0))
        for lap, gate, t, fin in h.two_lap_race():
            await controller.handle_event(h.racedata(lap, gate, t, fin))
        await controller.handle_event(h.status("race finished"))
    r = await client.post(
        "/races/bulk",
        data={
            "race_ids": ["1", "2"],
            "action": "update",
            "track_id": "900",
            "track_name": "Other",
            "scene_id": "8",
            "track_source": "official",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "2+runs" in loc or "2%20runs" in loc
    assert "track" in loc and "Other" in loc
    rows = (await client.get("/api/races")).json()
    assert all(x["track_id"] == 900 for x in rows) and sum(x["is_best"] for x in rows) == 1
    page = (await client.get("/races")).text
    assert 'class="select-all"' in page and 'class="since"' in page and "/static/races.js" in page


async def test_pace_setting_roundtrip(client: AsyncClient) -> None:
    assert "SPLITTER_PACE_YELLOW_S = 2.0" in (await client.get("/")).text
    r = await client.post(
        "/settings",
        data={
            "game_host": "",
            "game_port": "60003",
            "brand_name": "Splitter",
            "time_format": "seconds",
            "pace_yellow_s": "3.5",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "SPLITTER_PACE_YELLOW_S = 3.5" in (await client.get("/")).text
    assert 'name="pace_yellow_s"' in (await client.get("/settings")).text


async def test_header_has_link_and_game_indicators(client: AsyncClient) -> None:
    """#7: the page's link to the server and the server's link to the game are separate."""
    html = (await client.get("/races")).text
    assert 'id="link-dot"' in html and 'id="link-label"' in html
    assert 'id="game-dot"' in html and 'id="game-label"' in html
    assert "/static/link.js?v=" in html
    # Every page carries the link; the live page must not open a second socket.
    live = (await client.get("/")).text
    assert "/static/link.js?v=" in live and 'id="ws-state"' not in live


async def test_install_controls_are_gated_on_environment(client: AsyncClient) -> None:
    """#6/#8: the shell's webview flags itself; installed PWAs are detected robustly."""
    html = (await client.get("/")).text
    assert "window.SPLITTER_DESKTOP" in html and 'classList.add("desktop")' in html
    assert 'params.get("source") === "pwa"' in html
    assert "appinstalled" in html and "env.browserOnly()" in html


async def _fly_one(client: AsyncClient, scale: float = 1.0) -> None:
    controller = client.app.state.controller  # type: ignore[attr-defined]
    await controller.handle_event(h.session())
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    for lap, gate, t, fin in h.two_lap_race(scale=scale):
        await controller.handle_event(h.racedata(lap, gate, t, fin))
    await controller.handle_event(h.status("race finished"))


async def test_track_page_seeds_sections_and_the_editor_saves_them(client: AsyncClient) -> None:
    await _fly_one(client)
    await _fly_one(client, scale=1.1)
    url = "/tracks/detail?track_id=500&quad_model=0&laps=3"
    html = (await client.get(url)).text
    assert 'id="sections-card"' in html and "auto-suggested" in html
    assert "G3" in html and "S/F" in html  # lap-relative gate labels
    assert 'id="gate-strip"' in html and "/tracks/500/sections" not in html.split("<script")[0]
    # The gate table is still there, collapsed at the bottom.
    assert "gate-details" in html and html.index("gate-details") > html.index("sections-card")

    # Save a two-section layout (3 segments per lap in the test race).
    r = await client.post(
        "/tracks/500/sections",
        json={
            "count": 3,
            "sections": [
                {"name": "Opening", "first": 1, "last": 2},
                {"name": "", "first": 3, "last": 3},
            ],
        },
    )
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["sections"]] == ["Opening", "Section 2"]
    html = (await client.get(url)).text
    assert "Opening" in html and "auto-suggested" not in html
    # Gaps are refused.
    r = await client.post(
        "/tracks/500/sections", json={"count": 3, "sections": [{"first": 1, "last": 1}]}
    )
    assert r.status_code == 400
    # Reset drops the layout; the next view suggests again.
    assert (await client.post("/tracks/500/sections/reset")).status_code == 200
    assert "auto-suggested" in (await client.get(url)).text


async def test_race_page_shows_sections_against_the_pb(client: AsyncClient) -> None:
    await _fly_one(client)  # PB
    await _fly_one(client, scale=1.1)  # slower, compared to it
    await client.get("/tracks/detail?track_id=500&quad_model=0&laps=3")  # seeds the sections
    html = (await client.get("/races/2")).text
    assert "<h2>Sections</h2>" in html and "Lap 1" in html and "Lap 2" in html
    assert "+0.200" in html  # a 2 s segment flown 10% slower than the PB
    assert "Gate crossings" in html and html.index("Gate crossings") > html.index(
        "<h2>Sections</h2>"
    )
