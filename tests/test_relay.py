"""Sync phase 5: a node relays its live feed to the web and takes the web's
commands. The transport is a queue pair in one process; the real websocket
route is covered separately with Starlette's test client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from splitter.app import create_app
from splitter.config import Config
from splitter.db import repos
from splitter.live.relay import RelayError, RelayNode
from splitter.sync.relay import ChannelClosed, relay_url
from tests import helpers as h
from tests.sync_fixtures import WEB_URL, Side, configure
from tests.test_controller import fly
from tests.test_web import _FakeCatalog

_EOF = "\x00closed"


class PipeEnd:
    """One end of an in-memory duplex channel."""

    def __init__(self, inbox: asyncio.Queue[str], outbox: asyncio.Queue[str]) -> None:
        self._in, self._out = inbox, outbox
        self.close_code: int | None = None

    async def send(self, text: str) -> None:
        await self._out.put(text)

    async def recv(self) -> str:
        text = await self._in.get()
        if text.startswith(_EOF):
            _, _, rest = text.partition(":")
            code, _, reason = rest.partition(":")
            raise ChannelClosed(int(code) if code else None, reason)
        return text

    async def close(self, code: int | None = None, reason: str = "") -> None:
        await self._out.put(f"{_EOF}:{code or ''}:{reason}")


def pipe() -> tuple[PipeEnd, PipeEnd]:
    a: asyncio.Queue[str] = asyncio.Queue()
    b: asyncio.Queue[str] = asyncio.Queue()
    return PipeEnd(a, b), PipeEnd(b, a)


class FakeWeb:
    """What the fake transport does on the web's behalf: the auth check and
    the registry bookkeeping the ``/ws/relay`` route would do."""

    def __init__(self, web: Side) -> None:
        self.web = web
        self.connects = 0
        self.tasks: list[asyncio.Task[None]] = []

    async def connect(self, url: str, headers: dict[str, str]) -> PipeEnd:
        self.connects += 1
        assert url == relay_url(WEB_URL)
        node_end, web_end = pipe()
        token = self.web.state.settings.get("ingest_token")
        if headers.get("Authorization") != f"Bearer {token}":
            await web_end.close(4401, "bad token")
            return node_end
        node = RelayNode(headers["X-Splitter-Node-Id"], headers["X-Splitter-Node"], web_end)
        self.web.state.relays.add(node)
        async with self.web.state.session_factory() as db:
            await repos.note_node(db, node.node_id, node.name, 0, False)

        async def serve() -> None:
            try:
                await node.serve()
            except ChannelClosed:
                pass
            finally:
                self.web.state.relays.remove(node)

        self.tasks.append(asyncio.create_task(serve()))
        return node_end


@pytest.fixture
async def fake_web(web: Side) -> AsyncIterator[FakeWeb]:
    fw = FakeWeb(web)
    yield fw
    for t in fw.tasks:
        t.cancel()
    await asyncio.gather(*fw.tasks, return_exceptions=True)


@pytest.fixture
async def relay_node(tmp_path: Any, web: Side, fake_web: FakeWeb) -> AsyncIterator[Side]:
    app = create_app(Config(database_url=f"sqlite+aiosqlite:///{tmp_path}/node.db"))
    app.state.catalog = _FakeCatalog()
    app.state.sync_client = lambda url: AsyncClient(
        transport=ASGITransport(app=web.app), base_url=url
    )
    app.state.sync_autorun = False
    app.state.relay_connect = fake_web.connect
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://node") as c,
    ):
        await configure(c, url=WEB_URL, token=web.state.settings.get("ingest_token"))
        task = asyncio.create_task(app.state.relay.run())
        yield Side(app, c)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def switch(node: Side, on: bool) -> None:
    r = await node.client.post(
        "/settings",
        data={
            "upstream_url": node.state.settings.get("upstream_url"),
            "upstream_token": node.state.settings.get("upstream_token"),
            "relay_enabled": "1" if on else "0",
            "node_name": "gaming pc",
            "telemetry_enabled": "1",
            "telemetry_store_hz": "20",
            "event_log_keep": "500",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303


async def until(pred: Any, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never came true")


async def test_relay_is_off_until_switched_on(web: Side, relay_node: Side) -> None:
    relay = relay_node.state.relay
    await asyncio.sleep(0.05)
    assert relay.state == "off" and not relay.enabled and len(web.state.relays) == 0
    assert relay_node.state.controller.snapshot()["relay"]["state"] == "off"
    r = await relay_node.client.get("/settings")
    assert 'name="relay_enabled"' in r.text


async def test_relay_mirrors_the_feed_and_takes_commands(
    web: Side, relay_node: Side, fake_web: FakeWeb
) -> None:
    node_id = relay_node.state.settings.get("node_id")
    ctl, relay = relay_node.state.controller, relay_node.state.relay
    web_pages = web.state.hub.subscribe()
    await switch(relay_node, True)
    await until(lambda: web.state.relays.get(node_id) is not None and relay.connected)
    rn = web.state.relays.get(node_id)
    assert rn is not None and rn.name == "gaming pc"
    await until(lambda: "session" in rn.snapshot)  # the snapshot sent on connect
    assert any(m["type"] == "nodes" for m in h.drain(web_pages))

    # The node's live feed reaches a browser on the web.
    browser = rn.hub.subscribe()
    await ctl.handle_event(h.session())
    await fly(ctl, imu=True)
    await until(lambda: any(json.loads(m)["type"] == "race_finished" for m in list(browser._queue)))  # type: ignore[attr-defined]
    types = [m["type"] for m in h.drain(browser)]
    assert "race_started" in types and "crossing" in types and "race_finished" in types
    assert rn.snapshot["race"] is None and rn.snapshot["last_result"]

    # Commands from the web's Live page act on the node.
    r = await web.client.post(
        f"/api/relay/{node_id}/session",
        json={"track_name": "Sticky Track", "track_id": 501, "scene_id": 16, "quad_type": "Whoop"},
    )
    assert r.status_code == 200 and r.json()["track_id"] == 501
    assert ctl.session.track_id == 501 and ctl.session.track_name == "Sticky Track"
    r = await web.client.post(f"/api/relay/{node_id}/session", json={"track_name": "typed"})
    assert r.status_code == 400 and "online id" in r.json()["detail"]
    r = await web.client.post(f"/api/relay/{node_id}/capture", json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"enabled": False} and ctl.capture is False
    r = await web.client.post(f"/api/relay/{node_id}/race/abort", json={"in_game": False})
    assert r.status_code == 200 and r.json()["aborted"] is False

    # The pages on the web.
    r = await web.client.get(f"/live/{node_id}")
    assert r.status_code == 200 and "relayed" in r.text and "gaming pc" in r.text
    assert f'"/ws/live/{node_id}"' in r.text and f'"/api/relay/{node_id}"' in r.text
    assert 'id="ind-game"' in r.text  # the node's game indicator, even on a web
    r = await web.client.get("/live/nodes")
    assert r.status_code == 200 and "gaming pc" in r.text
    assert (await web.client.get("/api/relay")).json()["connected"][0]["node_id"] == node_id
    r = await web.client.get("/races")
    assert f'href="/live/{node_id}"' in r.text  # "Receiving from gaming pc [live]"
    # Web mode: home is the one relaying node.
    async with web.state.session_factory() as db:
        await web.state.settings.set(db, "web_mode", "1")
    r = await web.client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/live/{node_id}"
    r = await web.client.get("/races")
    assert f'href="/live/{node_id}"' in r.text and ">Live</a>" in r.text

    # Switching the relay off hangs up; the web forgets the node.
    await switch(relay_node, False)
    await until(lambda: relay.state == "off" and len(web.state.relays) == 0)
    r = await web.client.get(f"/live/{node_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/live/nodes")
    r = await web.client.post(f"/api/relay/{node_id}/capture", json={"enabled": True})
    assert r.status_code == 404
    r = await web.client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/races"


async def test_relay_refused_by_the_web_retries_slowly(
    web: Side, relay_node: Side, fake_web: FakeWeb
) -> None:
    relay = relay_node.state.relay
    await configure(relay_node.client, url=WEB_URL, token="wrong")
    await switch(relay_node, True)
    await until(lambda: relay.state == "refused")
    assert relay.last_error == "bad token" and fake_web.connects == 1
    await asyncio.sleep(0.1)
    assert fake_web.connects == 1  # no hammering
    # The right token: straight back in.
    await configure(relay_node.client, url=WEB_URL, token=web.state.settings.get("ingest_token"))
    await switch(relay_node, True)
    await until(lambda: relay.connected)


async def test_relay_node_folds_messages_and_times_out_commands() -> None:
    node_end, web_end = pipe()
    rn = RelayNode("abc", "pc", web_end)
    rn.handle({"type": "snapshot", "data": {"session": {"known": False}, "race": None}})
    rn.handle({"type": "race_started", "data": {"id": 7}})
    rn.handle({"type": "crossing", "data": {"cumulative_ms": 1200, "starts_lap": 1}})
    assert rn.snapshot["race"]["id"] == 7 and rn.snapshot["race"]["lap_start_ms"] == 1200
    rn.handle({"type": "race_finished", "data": {"id": 7, "total_ms": 9000}})
    assert rn.snapshot["race"] is None and rn.snapshot["last_result"]["total_ms"] == 9000
    # A command nobody answers.
    import splitter.live.relay as mod

    old = mod.COMMAND_TIMEOUT_S
    mod.COMMAND_TIMEOUT_S = 0.05
    try:
        with pytest.raises(RelayError) as ei:
            await rn.command("capture", {"enabled": True})
        assert ei.value.status == 504
    finally:
        mod.COMMAND_TIMEOUT_S = old
    sent = json.loads(await node_end.recv())
    assert sent["type"] == "capture" and sent["id"] == 1
    # A refused one carries the node's status.
    fut = asyncio.ensure_future(rn.command("session", {"track_name": "x"}))
    await asyncio.sleep(0)
    rn.handle({"type": "reply", "id": 2, "ok": False, "status": 400, "error": "no id"})
    with pytest.raises(RelayError) as ei:
        await fut
    assert ei.value.status == 400 and ei.value.detail == "no id"


def test_relay_url() -> None:
    assert relay_url("https://splitter.example.net/") == "wss://splitter.example.net/ws/relay"
    assert relay_url("http://192.168.1.10:8100") == "ws://192.168.1.10:8100/ws/relay"


def test_relay_websocket_route_auth_and_feed(tmp_path: Any) -> None:
    """The real socket: refused without a token, then a node's snapshot fans
    out to a browser on /ws/live/<node>."""
    app = create_app(Config(database_url=f"sqlite+aiosqlite:///{tmp_path}/ws.db"))
    app.state.catalog = _FakeCatalog()
    app.state.sync_autorun = False
    with TestClient(app) as c:
        with c.websocket_connect("/ws/relay") as ws, pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
        assert ei.value.code == 4403  # receiving off
        r = c.post("/settings/ingest-token", data={"action": "generate"}, follow_redirects=False)
        assert r.status_code == 303
        token = app.state.settings.get("ingest_token")
        with (
            c.websocket_connect("/ws/relay", headers={"Authorization": "Bearer nope"}) as ws,
            pytest.raises(WebSocketDisconnect) as ei,
        ):
            ws.receive_text()
        assert ei.value.code == 4401
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Splitter-Node-Id": "node-1",
            "X-Splitter-Node": "gaming pc",
        }
        with c.websocket_connect("/ws/relay", headers=headers) as ws:
            ws.send_text(json.dumps({"type": "snapshot", "data": {"session": {"known": False}}}))
            with c.websocket_connect("/ws/live/node-1") as browser:
                first = json.loads(browser.receive_text())
                assert first["type"] == "snapshot"
                # The browser's connect asked the node for a fresh snapshot.
                assert json.loads(ws.receive_text())["type"] == "snapshot"
                ws.send_text(json.dumps({"type": "crossing", "data": {"gate": 2}}))
                got = json.loads(browser.receive_text())
                while got["type"] == "snapshot":
                    got = json.loads(browser.receive_text())
                assert got == {"type": "crossing", "data": {"gate": 2}}
            assert c.get("/api/relay").json()["connected"][0]["name"] == "gaming pc"
        with c.websocket_connect("/ws/live/node-1") as browser:
            assert json.loads(browser.receive_text())["type"] == "node"
