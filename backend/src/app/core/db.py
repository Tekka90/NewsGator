"""Async SQLAlchemy engine/session setup."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
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


def _unwrap_sqlite_conn(dbapi_conn: object) -> object:
    """Unwrap aiosqlite/SQLAlchemy layers down to the raw sqlite3.Connection."""
    import sqlite3

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
    return raw


def _configure_sqlite_on_connect(dbapi_conn: object, _record: object) -> None:
    """Per-connection SQLite setup: WAL mode, busy timeout, sqlite-vec extension."""
    import sqlite3

    raw = _unwrap_sqlite_conn(dbapi_conn)
    if not isinstance(raw, sqlite3.Connection):
        return
    # WAL lets readers proceed during a writer's transaction; busy_timeout makes
    # writers wait instead of failing with "database is locked" under the
    # concurrent scheduler / LLM worker / API load.
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=30000")
    raw.execute("PRAGMA synchronous=NORMAL")
    try:
        import sqlite_vec

        raw.enable_load_extension(True)
        sqlite_vec.load(raw)
    except Exception:
        pass  # extension unavailable → in-memory vector fallback


def init_engine(database_url: str) -> AsyncEngine:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    if database_url.startswith("sqlite"):
        event.listens_for(_engine.sync_engine, "connect")(_configure_sqlite_on_connect)
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


async def commit_with_retry(
    session: AsyncSession,
    prepare: Callable[[], Awaitable[None]] | None = None,
    attempts: int = 4,
    base_delay: float = 0.25,
) -> None:
    """Commit, retrying transient SQLite 'database is locked' errors.

    WAL + busy_timeout cover most contention, but a long writer (LLM pipeline
    flush, freeze sweep, retention) can still outlast the timeout under load —
    the pipeline worker, scheduler jobs, and API handlers all share one writer
    slot. Roll back and retry with backoff instead of killing the job.

    A rollback discards the session's pending objects, so pass ``prepare`` — a
    callback that re-queues the work (e.g. re-emitting an activity event) —
    which is re-invoked before each retry. On exhaustion the error propagates
    (the sweep is idempotent and self-heals on its next run).
    """
    for attempt in range(attempts):
        if attempt and prepare is not None:
            await prepare()
        try:
            await session.commit()
            return
        except OperationalError as exc:
            if "locked" not in str(exc.orig or exc).lower() or attempt == attempts - 1:
                raise
            await session.rollback()
            await asyncio.sleep(base_delay * (2**attempt))
