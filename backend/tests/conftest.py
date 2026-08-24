"""Test fixtures: per-test SQLite DB + httpx AsyncClient."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import db
from app.core.db import Base
from app.main import create_app


@pytest.fixture()
async def db_session(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Initialize a fresh DB and yield a session factory."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    db.init_engine(db_url)
    engine = db.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
async def client(db_session) -> AsyncIterator[AsyncClient]:
    app = create_app()
    # Bypass lifespan (would re-init engine with default URL / start scheduler)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


ADMIN = {"username": "admin", "password": "supersecret1"}


async def setup_admin(client: AsyncClient) -> None:
    r = await client.post("/api/auth/setup", json=ADMIN)
    assert r.status_code == 201, r.text
