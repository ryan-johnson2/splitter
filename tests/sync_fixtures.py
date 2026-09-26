"""Fixtures for sync tests: a web app and a node app in one process, the node's
uploader bound to the web through ASGITransport (no network), the background
loop off so tests drive ``process_once`` themselves."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from splitter.app import create_app
from splitter.config import Config
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
