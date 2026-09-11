"""Environment-driven configuration (deploy-time values only).

Runtime knobs (the game PC's address, player name, telemetry options) live
in the ``settings`` table and are edited from the Settings page.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Application configuration loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./data/splitter.db"
    host: str = "0.0.0.0"
    port: int = 8100

    @property
    def data_path(self) -> Path:
        if self.database_url.startswith("sqlite"):
            path = self.database_url.split("///", 1)[-1]
            if path and not path.startswith(":"):
                return Path(path).expanduser().parent
        return Path("./data")
