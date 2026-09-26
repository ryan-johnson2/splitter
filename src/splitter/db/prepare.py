"""What every process does to a database before using it: schema, identity, migrations.

Shared by the app's startup and by every CLI command that touches data, so a
run exported from the command line has the same uuid the running app gave it.
"""

from __future__ import annotations

import logging
from typing import Any

from splitter.db import repos
from splitter.db.engine import init_db
from splitter.db.runtime_settings import RuntimeSettings

log = logging.getLogger(__name__)


async def prepare(session_factory: Any) -> RuntimeSettings:
    """Create/upgrade the schema, load settings, mint this install's node id,
    give pre-0.6.0 runs a uuid, and convert legacy telemetry rows to blobs.
    Idempotent and resumable; returns the loaded settings."""
    await init_db(session_factory.kw["bind"])
    settings = RuntimeSettings()
    async with session_factory() as session:
        await settings.load(session)
        if not settings.get("node_id"):
            await settings.set(session, "node_id", repos.new_uuid())
            log.info("this install is node %s", settings.get("node_id"))
        touched = await repos.backfill_identity(session, settings.get("node_id"))
        if touched:
            log.info("gave %d existing runs a uuid", touched)
        converted = 0
        while n := await repos.migrate_telemetry(session):
            converted += n
            log.info("telemetry migration: %d runs converted so far", converted)
        if converted:
            log.info(
                "telemetry migration done: %d runs; `splitter migrate-telemetry --drop` "
                "removes the empty legacy table",
                converted,
            )
    return settings
