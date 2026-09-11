"""Async engine / session factory with SQLite WAL mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from splitter.db.models import Base


def create_engine(database_url: str) -> AsyncEngine:
    """Create the async engine, enabling WAL + foreign keys for SQLite."""
    if database_url.startswith("sqlite"):
        # Split at the FIRST "///": four slashes mean an absolute path.
        path = database_url.split("///", 1)[-1]
        if path and not path.startswith(":"):
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"timeout": 15} if database_url.startswith("sqlite") else {}
    engine = create_async_engine(database_url, connect_args=connect_args)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables (idempotent) and add any columns new since the DB was made.

    Only additive changes are handled here (``ALTER TABLE ... ADD COLUMN``).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn: Any) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    for table in Base.metadata.tables.values():
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
            ddl += column.type.compile(dialect=conn.dialect)
            default = getattr(column.default, "arg", None)
            if isinstance(default, str):
                ddl += f" DEFAULT '{default}'"
            elif isinstance(default, bool):
                ddl += f" DEFAULT {int(default)}"
            elif isinstance(default, int | float):
                ddl += f" DEFAULT {default}"
            conn.execute(text(ddl))
