"""Favicon proxy tests: fetch, cache, failure, validation."""

import httpx
import pytest
from httpx import AsyncClient
from tests.conftest import ADMIN, setup_admin

from app.api import favicons

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    favicons._cache.clear()


async def test_favicon_proxied_and_cached(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    calls = 0

    async def fake_fetch(host: str) -> tuple[bytes, str]:
        nonlocal calls
        calls += 1
        assert host == "news.example.com"
        return PNG, "image/png"

    monkeypatch.setattr(favicons, "_fetch_favicon", fake_fetch)

    r = await client.get("/api/favicon?host=news.example.com")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG
    assert "max-age" in r.headers["cache-control"]

    # Second call served from cache — no new fetch
    r = await client.get("/api/favicon?host=news.example.com")
    assert r.status_code == 200
    assert calls == 1


async def test_favicon_failure_is_404_and_cached(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    calls = 0

    async def fake_fetch(host: str) -> tuple[bytes, str]:
        nonlocal calls
        calls += 1
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(favicons, "_fetch_favicon", fake_fetch)

    assert (await client.get("/api/favicon?host=broken.example.com")).status_code == 404
    assert (await client.get("/api/favicon?host=broken.example.com")).status_code == 404
    assert calls == 1  # failures cached too


async def test_favicon_host_validation(client: AsyncClient) -> None:
    await setup_admin(client)
    assert (await client.get("/api/favicon?host=bad host!")).status_code == 400
    assert (await client.get("/api/favicon?host=..%2Fetc")).status_code == 400


async def test_favicon_requires_auth(client: AsyncClient) -> None:
    await setup_admin(client)
    await client.post("/api/auth/logout")
    client.cookies.clear()
    assert (await client.get("/api/favicon?host=x.com")).status_code == 401
    # ?token= works — <img> tags can't send headers
    r = await client.post("/api/auth/login", json=ADMIN)
    token = r.json()["token"]
    client.cookies.clear()
    assert (
        await client.get(f"/api/favicon?host=bad host!&token={token}")
    ).status_code == 400  # authenticated → validation, not 401
