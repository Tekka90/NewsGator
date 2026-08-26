"""Tests for the settings service-probe endpoints (test-qdrant / test-readeck)."""

import httpx
import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.core.config import settings as env_settings


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(env_settings, "vector_backend", "sqlite_vec")
    monkeypatch.setattr(env_settings, "qdrant_url", None)
    monkeypatch.setattr(env_settings, "qdrant_api_key", None)
    monkeypatch.setattr(env_settings, "readeck_base_url", None)
    monkeypatch.setattr(env_settings, "readeck_token", None)


async def test_test_qdrant_not_configured(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post("/api/settings/test-qdrant")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "not 'qdrant'" in body["errors"][0]


async def test_test_qdrant_reachable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(env_settings, "vector_backend", "qdrant")
    monkeypatch.setattr(env_settings, "qdrant_url", "http://qdrant.test:6333")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"title": "qdrant", "version": "1.12.0"}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url == "http://qdrant.test:6333/version"
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    r = await client.post("/api/settings/test-qdrant")
    body = r.json()
    assert body["ok"] is True
    assert body["version"]["version"] == "1.12.0"


async def test_test_readeck_not_configured(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post("/api/settings/test-readeck")
    assert r.json()["ok"] is False


async def test_test_readeck_ok_and_missing_write_role(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(env_settings, "readeck_base_url", "http://readeck.test")
    monkeypatch.setattr(env_settings, "readeck_token", "tok")

    profile = {
        "provider": {"roles": ["bookmarks:read", "bookmarks:write"]},
        "user": {"username": "stephane"},
    }

    class FakeResp:
        status_code = 200

        def json(self):
            return profile

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url == "http://readeck.test/api/profile"
            assert headers["Authorization"] == "Bearer tok"
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    r = await client.post("/api/settings/test-readeck")
    body = r.json()
    assert body["ok"] is True
    assert body["user"] == "stephane"

    # Read-only token → ok False with a clear error
    profile["provider"]["roles"] = ["bookmarks:read"]
    r = await client.post("/api/settings/test-readeck")
    body = r.json()
    assert body["ok"] is False
    assert "bookmarks:write" in body["errors"][0]


async def test_test_readeck_requires_admin(client: AsyncClient) -> None:
    assert (await client.post("/api/settings/test-readeck")).status_code == 401
    assert (await client.post("/api/settings/test-qdrant")).status_code == 401
