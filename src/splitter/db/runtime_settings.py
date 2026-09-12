"""Hot-reloadable runtime settings backed by the ``settings`` table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from splitter.db.models import Setting
from splitter.util import utcnow

DEFAULTS: dict[str, str] = {
    # The gaming PC. The game binds its LAN IP (never loopback), so this must
    # be that address even when Splitter runs on the same machine.
    "game_host": "",
    "game_port": "60003",
    # Which pilot in the racedata snapshot is "me". Empty = learn it from the
    # first session/player event, else the only pilot in the snapshot.
    "player_name": "",
    "auto_connect": "1",
    # IMU trace: keep it, and at what rate (the game sends 60 Hz).
    "telemetry_enabled": "1",
    "telemetry_store_hz": "20",
    # Raw frame log for protocol debugging (imu sampled to ~1 per 5 s).
    "event_log_enabled": "1",
    "event_log_keep": "5000",
    # Last known session, reused when the game doesn't say (single player).
    "last_track_name": "",
    "last_scenery": "",
    "last_track_id": "0",
    "last_scene_id": "0",
    "last_track_source": "",
    "last_quad_type": "",
    "last_quad_size": "",
    "last_quad_model_id": "0",
    "last_quad_class_id": "0",
    "last_race_mode": "",
    "last_race_laps": "0",
    "brand_name": "Splitter",
    # How times are shown: seconds (like the game) | mmss | both. See core/timeparse.
    "time_format": "seconds",
}


class RuntimeSettings:
    """In-memory view of the settings table."""

    def __init__(self) -> None:
        self._values: dict[str, str] = dict(DEFAULTS)

    async def load(self, session: AsyncSession) -> None:
        rows = (await session.execute(select(Setting))).scalars().all()
        self._values = dict(DEFAULTS)
        for row in rows:
            self._values[row.key] = row.value

    async def set(self, session: AsyncSession, key: str, value: str) -> None:
        existing = await session.get(Setting, key)
        now = utcnow()
        if existing is None:
            session.add(Setting(key=key, value=value, updated_at=now))
        else:
            existing.value = value
            existing.updated_at = now
        await session.commit()
        self._values[key] = value

    async def set_many(self, session: AsyncSession, values: dict[str, str]) -> None:
        for key, value in values.items():
            await self.set(session, key, value)

    def get(self, key: str) -> str:
        return self._values.get(key, DEFAULTS.get(key, ""))

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(DEFAULTS.get(key, "0"))

    def get_float(self, key: str) -> float:
        try:
            return float(self.get(key))
        except ValueError:
            return float(DEFAULTS.get(key, "0"))

    def get_bool(self, key: str) -> bool:
        return self.get(key).strip().lower() in ("1", "true", "yes", "on")

    def all(self) -> dict[str, str]:
        return dict(self._values)
