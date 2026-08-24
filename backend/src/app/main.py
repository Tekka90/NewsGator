"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api import auth, categories, feeds, ops
from app.api import settings as settings_api
from app.core.config import settings
from app.core.db import get_engine, get_session, init_engine
from app.models import SEED_CATEGORIES, Category


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
    app.include_router(feeds.router, prefix="/api")
    app.include_router(categories.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    return app


async def _ensure_schema_and_seed() -> None:
    """Milestone 1 simplification: create_all + seed categories.

    Alembic revision shipped alongside; create_all keeps first-run trivial and is
    removed once `alembic upgrade head` is wired into startup (Milestone 8).
    """
    from app.core.db import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async for session in get_session():
        existing = await session.scalar(select(Category.id).limit(1))
        if existing is None:
            session.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await session.commit()
        # Runtime overrides from the SETTING table (SPEC: never hardcoded)
        from app.api.settings import apply_overrides_at_startup

        await apply_overrides_at_startup(session)
        break


app = create_app()
