from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitter.db.engine import create_engine, create_session_factory, init_db
from splitter.db.runtime_settings import RuntimeSettings
from splitter.game.catalog import TrackRef
from splitter.game.controller import RaceController
from splitter.live.hub import LiveHub

# What the online lists "know" in tests: exact-name → id, like TrackCatalog.resolve.
KNOWN_TRACKS: dict[str, TrackRef] = {
    "practice loop": TrackRef("community", 500, 29, "Practice Loop", "Beginner", "Ryan"),
    "sticky track": TrackRef("official", 501, 16, "Sticky Track", "Intermediate"),
}


async def fake_resolve(name: str) -> TrackRef | None:
    return KNOWN_TRACKS.get(name.strip().lower())


@pytest.fixture
async def session_factory(tmp_path: Any) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def settings(session_factory: async_sessionmaker[AsyncSession]) -> RuntimeSettings:
    s = RuntimeSettings()
    async with session_factory() as db:
        await s.load(db)
    return s


@pytest.fixture
def hub() -> LiveHub:
    return LiveHub()


@pytest.fixture
def controller(
    settings: RuntimeSettings, session_factory: async_sessionmaker[AsyncSession], hub: LiveHub
) -> RaceController:
    return RaceController(
        settings,
        session_factory,
        hub,
        status_provider=lambda: {"connected": True},
        resolve_track=fake_resolve,
    )
