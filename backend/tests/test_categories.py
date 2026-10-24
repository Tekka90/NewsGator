"""Categories + health tests."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.core.db import get_session
from app.models import SEED_CATEGORIES, Category


async def _seed_categories() -> None:
    async for s in get_session():
        existing = await s.scalar(select(Category.id).limit(1))
        if existing is None:
            s.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await s.commit()
        break


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "story_history" in r.json()["features"]
    assert r.json()["commit"]
    assert r.json()["version"]


def test_version_comes_from_release_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from app import version

    monkeypatch.setenv("APP_VERSION", "v1.6.0")
    assert importlib.reload(version).VERSION == "1.6.0"
    monkeypatch.delenv("APP_VERSION")
    assert importlib.reload(version).VERSION == "dev"


async def test_categories_crud(client: AsyncClient) -> None:
    await setup_admin(client)
    await _seed_categories()

    r = await client.get("/api/categories")
    names = [c["name"] for c in r.json()]
    assert "Tech" in names and "Uncategorized" in names

    r = await client.post("/api/categories", json={"name": "AI"})
    assert r.status_code == 201
    cat_id = r.json()["id"]

    r = await client.post("/api/categories", json={"name": "AI"})
    assert r.status_code == 409

    r = await client.patch(f"/api/categories/{cat_id}", json={"name": "AI & ML"})
    assert r.json()["name"] == "AI & ML"

    r = await client.delete(f"/api/categories/{cat_id}")
    assert r.status_code == 204

    # 'Uncategorized' is protected
    r = await client.get("/api/categories")
    unc = next(c for c in r.json() if c["name"] == "Uncategorized")
    assert (await client.delete(f"/api/categories/{unc['id']}")).status_code == 400
