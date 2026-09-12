"""FastAPI application: the web/live UI plus the supervised game connection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from splitter.config import Config
from splitter.core import quads
from splitter.db import repos
from splitter.db.engine import create_engine, create_session_factory, init_db
from splitter.db.runtime_settings import RuntimeSettings
from splitter.game.bridge import GameBridge
from splitter.game.catalog import TrackCatalog
from splitter.game.controller import RaceController
from splitter.live.hub import LiveHub
from splitter.version import __version__

log = logging.getLogger(__name__)


async def _supervise(name: str, coro_factory: Any) -> None:
    """Run a task forever; log and restart (with backoff) on unexpected exit."""
    delay = 2.0
    while True:
        try:
            await coro_factory()
            log.warning("%s exited; restarting in %.0fs", name, delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed; restarting in %.0fs", name, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        engine = create_engine(cfg.database_url)
        await init_db(engine)
        session_factory = create_session_factory(engine)

        settings = RuntimeSettings()
        async with session_factory() as session:
            await settings.load(session)
            # Any race left "running" by a crash is an abort.
            for race in await repos.list_races(session, repos.RaceFilters(status="running")):
                race.status = "aborted"
            await session.commit()
            pruned = await repos.prune_event_log(session, settings.get_int("event_log_keep"))
            if pruned:
                log.info("pruned %d old event log rows", pruned)

        hub = LiveHub()
        bridge: GameBridge
        # Online track lists for the picker; tests replace it with a fake.
        if not hasattr(app.state, "catalog"):
            app.state.catalog = TrackCatalog()
        catalog = app.state.catalog
        game_cat = quads.catalog()
        log.info(
            "quad/scene catalog: %d models, %d scenes (%s)",
            len(game_cat.models),
            len(game_cat.scenes),
            game_cat.generated_at or "no bundled file",
        )
        controller = RaceController(
            settings,
            session_factory,
            hub,
            status_provider=lambda: bridge.status(),
            resolve_track=catalog.resolve,
        )
        controller.load_sticky_session()
        await controller.refresh_reference()

        async def on_state(_b: GameBridge) -> None:
            controller.broadcast_status()

        bridge = GameBridge(on_event=controller.handle_event, on_state=on_state)
        if settings.get_bool("auto_connect") and settings.get("game_host"):
            bridge.configure(settings.get("game_host"), settings.get_int("game_port"))

        app.state.config = cfg
        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.hub = hub
        app.state.bridge = bridge
        app.state.controller = controller

        task = asyncio.create_task(_supervise("game-bridge", bridge.run), name="game-bridge")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await engine.dispose()

    app = FastAPI(title="Splitter", lifespan=lifespan)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="static",
    )

    from splitter.web.routes import api, history, live, protocol, settings_page, tracks

    app.include_router(live.router)
    app.include_router(api.router)
    app.include_router(history.router)
    app.include_router(tracks.router)
    app.include_router(settings_page.router)
    app.include_router(protocol.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        bridge: GameBridge = app.state.bridge
        controller: RaceController = app.state.controller
        return {
            "ok": True,
            "version": __version__,
            "game": bridge.status(),
            "race_active": controller.race_active,
            "clients": app.state.hub.clients,
        }

    return app
