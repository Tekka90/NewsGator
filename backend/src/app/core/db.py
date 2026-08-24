"""Async SQLAlchemy engine/session setup."""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _load_vec_on_connect(dbapi_conn: object, _record: object) -> None:
    """Load sqlite-vec on each pooled connection (extensions are per-connection)."""
    try:
        import sqlite3

        import sqlite_vec

        raw = dbapi_conn
        for _ in range(5):
            if isinstance(raw, sqlite3.Connection):
                break
            nxt = getattr(raw, "driver_connection", None) or getattr(
                raw, "_connection", None
            )
            if nxt is None or nxt is raw:
                break
            raw = nxt
        if isinstance(raw, sqlite3.Connection):
            raw.enable_load_extension(True)
            sqlite_vec.load(raw)
    except Exception:
        pass  # extension unavailable → in-memory vector fallback


def init_engine(database_url: str) -> AsyncEngine:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    if database_url.startswith("sqlite"):
        event.listens_for(_engine.sync_engine, "connect")(_load_vec_on_connect)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    assert _engine is not None, "Engine not initialized — call init_engine() first"
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session."""
    assert _session_factory is not None, "Engine not initialized — call init_engine() first"
    async with _session_factory() as session:
        yield session
