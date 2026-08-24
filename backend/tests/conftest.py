"""Test fixtures: in-memory-ish SQLite app + httpx AsyncClient."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core import db
from app.core.db import Base
from app.main import create_app


@pytest.fixture()
async def client(tmp_path) -> AsyncIterator[AsyncClient]:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    db.init_engine(db_url)
    engine = db.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = create_app()
    # Bypass lifespan (would re-init engine with default URL)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    await engine.dispose()


ADMIN = {"username": "admin", "password": "supersecret1"}


async def setup_admin(client: AsyncClient) -> None:
    r = await client.post("/api/auth/setup", json=ADMIN)
    assert r.status_code == 201, r.text
