import pathlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory of a file-based SQLite DB so the very first
    connection doesn't fail with 'unable to open database file'. No-op for
    in-memory SQLite and non-SQLite backends."""
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite") and parsed.database and parsed.database != ":memory:":
        pathlib.Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


def build_engine(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    ensure_sqlite_dir(url)
    engine = create_async_engine(url)

    # SQLite needs WAL (concurrent reads while writing) and foreign_keys ON for
    # the projections -> campaigns cascade. No-op / harmless on other backends'
    # DBAPIs would error, so only wire it for sqlite URLs.
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, from the app-scoped factory
    set in the lifespan (so tests can point it at a temp DB)."""
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with maker() as session:
        yield session
