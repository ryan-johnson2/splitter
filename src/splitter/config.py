"""Environment-driven configuration (deploy-time values only).

Runtime knobs (the game PC's address, player name, telemetry options) live
in the ``settings`` table and are edited from the Settings page.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Application configuration loaded from environment / .env.

    ``SPLITTER_DATA_DIR`` (the desktop app sets it to its portable data folder)
    places the SQLite file there unless ``DATABASE_URL`` is given explicitly.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    data_dir: str = ""  # DATA_DIR; SPLITTER_DATA_DIR (the desktop app) takes precedence
    host: str = "0.0.0.0"
    port: int = 8100

    def model_post_init(self, _ctx: object) -> None:
        self.data_dir = os.environ.get("SPLITTER_DATA_DIR", "") or self.data_dir
        if not self.database_url:
            base = Path(self.data_dir).expanduser() if self.data_dir else Path("./data")
            self.database_url = f"sqlite+aiosqlite:///{base / 'splitter.db'}"

    @property
    def data_path(self) -> Path:
        if self.database_url.startswith("sqlite"):
            path = self.database_url.split("///", 1)[-1]
            if path and not path.startswith(":"):
                return Path(path).expanduser().parent
        return Path("./data")
