"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api import activity as activity_api
from app.api import auth, categories, chat, favicons, feeds, ops, stories, usage, users
from app.api import settings as settings_api
from app.core.config import settings
from app.core.db import get_engine, get_session, init_engine
from app.models import SEED_CATEGORIES, Category

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_engine(settings.database_url)
    await _ensure_schema_and_seed()
    if settings.environment != "test":
        from app.services.process import enqueue_backlog, start_worker, stop_worker
        from app.workers.scheduler import start_scheduler, stop_scheduler

        start_worker()
        # Crash recovery (invariant 7): requeue articles stuck mid-pipeline
        async for session in get_session():
            await enqueue_backlog(session)
            break
        start_scheduler()
        try:
            yield
        finally:
            stop_scheduler()
            await stop_worker()
    else:
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="NewsGator", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # SvelteKit dev server
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ops.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(feeds.router, prefix="/api")
    app.include_router(categories.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(stories.router, prefix="/api")
    app.include_router(activity_api.router, prefix="/api")
    app.include_router(favicons.router, prefix="/api")
    app.include_router(usage.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    return app


def _find_backend_dir() -> Path:
    """Locate the directory holding alembic.ini + the alembic/ scripts dir.

    Resolution order:
    1. ``NEWSGATOR_BACKEND_DIR`` env var (set by the Dockerfile to /app — the
       installed package lives in site-packages, so it can't be derived there).
    2. Walk upward from this file for a dir containing both alembic.ini and
       alembic/ (the dev layout: backend/src/app → backend/).
    3. Historical parents[2] assumption as a last resort.
    """
    import os

    env_dir = os.environ.get("NEWSGATOR_BACKEND_DIR")
    if env_dir:
        return Path(env_dir)
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").is_file() and (parent / "alembic").is_dir():
            return parent
    return Path(__file__).resolve().parents[2]


BACKEND_DIR = _find_backend_dir()
ALEMBIC_DIR = BACKEND_DIR / "alembic"

# Legacy detection: databases created by create_all (pre-Alembic adoption) have
# no alembic_version table. Each entry maps schema markers that must ALL be
# present to the revision such a DB should be stamped at, newest first.
_LEGACY_STAMPS: list[tuple[list[tuple[str, str]], str]] = [
    (
        [
            ("chat_message", "stories_json"),
            ("llm_usage", "estimated"),
            ("story", "readeck_bookmark_id"),
            ("feed", "backfill_days"),
            ("user", "story_filter"),
        ],
        "0011_chat_message",
    ),
    (
        [
            ("llm_usage", "estimated"),
            ("story", "readeck_bookmark_id"),
            ("feed", "backfill_days"),
            ("user", "story_filter"),
        ],
        "0010_llm_usage",
    ),
    (
        [
            ("story", "readeck_bookmark_id"),
            ("feed", "backfill_days"),
            ("user", "story_filter"),
        ],
        "0009_story_readeck",
    ),
    (
        [("feed", "backfill_days"), ("user", "story_filter"), ("story", "image_url")],
        "0008_feed_backfill_days",
    ),
    (
        [("user", "story_sort"), ("user", "story_filter"), ("story", "image_url")],
        "0007_user_story_filter",
    ),
    ([("user", "story_sort"), ("story", "image_url")], "0006_user_story_prefs"),
    ([("story", "image_url")], "0005_image_urls"),
    ([], "0004_cluster_tables"),  # tables exist; only columns differ per revision
]


def _db_state_sync(conn: object) -> tuple[str | None, bool]:
    """(current alembic revision, has user table) — sync, inside the engine."""
    import sqlalchemy as sa

    assert isinstance(conn, sa.engine.Connection)
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    current: str | None = None
    if "alembic_version" in tables:
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
        current = str(row[0]) if row else None
    return current, "user" in tables


def _legacy_stamp_revision(conn: object) -> str:
    """Newest revision whose schema markers are all present (legacy create_all DB)."""
    import sqlalchemy as sa

    assert isinstance(conn, sa.engine.Connection)
    for markers, rev in _LEGACY_STAMPS:
        if all(
            col in {str(r[1]) for r in conn.execute(sa.text(f"PRAGMA table_info({tbl})"))}
            for tbl, col in markers
        ):
            return rev
    return _LEGACY_STAMPS[-1][1]


async def _migrate_schema() -> None:
    """Bring the database to Alembic head.

    Runs the Alembic CLI (same as the Docker entrypoint) so behavior is identical
    in dev and containers. Legacy create_all DBs (no alembic_version table) are
    stamped at the newest revision their schema already matches, then upgraded.
    """
    import asyncio
    import sys

    alembic = [sys.executable, "-m", "alembic"]
    engine = get_engine()
    async with engine.connect() as conn:
        current, has_tables = await conn.run_sync(_db_state_sync)

    if current is None and has_tables:
        stamp = None
        async with engine.connect() as conn:
            stamp = await conn.run_sync(_legacy_stamp_revision)
        logger.info("Legacy DB without alembic_version — stamping at %s", stamp)
        proc = await asyncio.create_subprocess_exec(
            *alembic, "stamp", stamp, cwd=BACKEND_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"alembic stamp failed: {out.decode()}")

    proc = await asyncio.create_subprocess_exec(
        *alembic, "upgrade", "head", cwd=BACKEND_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed: {out.decode()}")
    if b"Running upgrade" in out:
        logger.info("Database schema migrated to head:\n%s", out.decode().strip())


def _create_vec_tables_sync(database_url: str) -> None:
    """Create vec0 tables via a synchronous sqlite3 connection to the DB file.

    Skipped for in-memory DBs (tests use the in-memory vector store anyway).
    """
    import logging

    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    path = database_url[len(prefix):]
    if path == ":memory:":
        return
    try:
        import sqlite3

        import sqlite_vec

        from app.services.vectorstore import EMBED_DIM

        conn = sqlite3.connect(path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        for table in ("vec_article", "vec_story"):
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                f"USING vec0(embedding float[{EMBED_DIM}])"
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "sqlite-vec unavailable, vector store falls back to in-memory: %s", exc
        )


async def _ensure_schema_and_seed() -> None:
    """Migrate schema to Alembic head, then create_all safety net + seed.

    Alembic owns the schema; the lifespan upgrades the DB to head at startup
    (legacy create_all DBs without alembic_version are stamped at the matching
    revision first). create_all then stays a no-op safety net. Skipped in tests
    (ENVIRONMENT=test): conftest builds the schema directly with create_all.
    """
    from app.core.db import Base

    engine = get_engine()
    if settings.environment != "test":
        await _migrate_schema()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # vec0 virtual tables (sqlite-vec) — create_all can't express these.
        # aiosqlite thread routing makes per-connection extension loading
        # unreliable, so create them with a direct synchronous connection to the
        # DB file. If the URL is in-memory or loading fails, the in-memory vector
        # fallback keeps the app running.
        _create_vec_tables_sync(settings.database_url)
    async for session in get_session():
        existing = await session.scalar(select(Category.id).limit(1))
        if existing is None:
            session.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await session.commit()
        # Runtime overrides from the SETTING table (SPEC: never hardcoded)
        from app.api.settings import apply_overrides_at_startup

        await apply_overrides_at_startup(session)
        # Initialize the configured vector backend (Qdrant collections /
        # sqlite-vec tables) now that VECTOR_BACKEND is final.
        from app.services.vectorstore import init_vector_store

        try:
            await init_vector_store(session)
        except Exception as exc:
            logger.warning("Vector store init failed (%s) — in-memory fallback", exc)
        break


app = create_app()
