"""Startup auto-migration tests: versioned, legacy (create_all), and empty DBs."""

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import db
from app.core.db import Base
from app.main import _ensure_schema_and_seed


@pytest.fixture()
async def engine_url(tmp_path, monkeypatch) -> AsyncIterator[str]:
    url = f"sqlite+aiosqlite:///{tmp_path}/auto.db"
    monkeypatch.setenv("DATABASE_URL", url)
    # Alembic's env.py reads `settings.database_url` (a module-level singleton) —
    # point it at this test's DB so the subprocess migrates the right file.
    from app.core.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    # conftest globally sets environment="test", which makes _ensure_schema_and_seed
    # skip _migrate_schema(). These tests specifically exercise the migration path,
    # so run them as a non-test environment (restored by monkeypatch afterwards).
    monkeypatch.setattr(settings, "environment", "migration-test")
    yield url
    if db._engine is not None:
        await db._engine.dispose()


def _version(path: Path) -> str | None:
    conn = sqlite3.connect(path)
    try:
        if not conn.execute(
            "select name from sqlite_master where name='alembic_version'"
        ).fetchone():
            return None
        row = conn.execute("select version_num from alembic_version").fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


async def test_empty_db_migrated_from_scratch(engine_url: str, tmp_path) -> None:
    db.init_engine(engine_url)
    await _ensure_schema_and_seed()
    path = tmp_path / "auto.db"
    assert _version(path) is not None  # stamped at head by the migrations
    assert "image_url" in _columns(path, "story")
    assert "image_url" in _columns(path, "article")


async def test_legacy_create_all_db_stamped_and_upgraded(
    engine_url: str, tmp_path
) -> None:
    """DB built by create_all (no alembic_version) → stamp 0004, upgrade to head."""
    db.init_engine(engine_url)
    engine = db.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    # Simulate a pre-0005 legacy DB: drop the columns create_all just added
    sync_conn = sqlite3.connect(tmp_path / "auto.db")
    sync_conn.execute("ALTER TABLE story DROP COLUMN image_url")
    sync_conn.execute("ALTER TABLE article DROP COLUMN image_url")
    sync_conn.execute("ALTER TABLE user DROP COLUMN story_sort")
    sync_conn.execute("ALTER TABLE user DROP COLUMN story_order")
    sync_conn.execute("ALTER TABLE user DROP COLUMN story_filter")
    sync_conn.execute("ALTER TABLE feed DROP COLUMN backfill_days")
    sync_conn.execute("ALTER TABLE story DROP COLUMN readeck_bookmark_id")
    # llm_usage (0010) and chat_message (0011) were added by later revisions —
    # a real pre-0005 DB never had them
    sync_conn.execute("DROP TABLE llm_usage")
    sync_conn.execute("DROP TABLE chat_message")
    sync_conn.commit()
    sync_conn.close()
    db.init_engine(engine_url)  # reconnect after the sync-side ALTER

    await _ensure_schema_and_seed()
    path = tmp_path / "auto.db"
    assert _version(path) is not None
    assert "image_url" in _columns(path, "story")
    assert "story_sort" in _columns(path, "user")
    assert "story_filter" in _columns(path, "user")
    assert "backfill_days" in _columns(path, "feed")
    assert "readeck_bookmark_id" in _columns(path, "story")
    assert "estimated" in _columns(path, "llm_usage")
    assert "stories_json" in _columns(path, "chat_message")


async def test_already_at_head_is_noop(engine_url: str, tmp_path) -> None:
    db.init_engine(engine_url)
    await _ensure_schema_and_seed()
    version = _version(tmp_path / "auto.db")
    # Second run: same version, no error
    await _ensure_schema_and_seed()
    assert _version(tmp_path / "auto.db") == version


async def _seed_check(engine_url: str) -> None:
    """Helper: categories seeded on first run."""
    db.init_engine(engine_url)
    await _ensure_schema_and_seed()
    factory = async_sessionmaker(db.get_engine())
    async with factory() as s:
        from sqlalchemy import select

        from app.models import Category

        assert (await s.scalar(select(Category.id).limit(1))) is not None
