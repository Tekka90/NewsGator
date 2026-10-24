"""Settings probes (test-qdrant / test-readeck) and threshold report."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.core.config import settings

_RealAsyncClient = httpx.AsyncClient


def _mock_upstream(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Route the probe's own outbound httpx client through `handler`."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording)
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


async def test_qdrant_probe_requires_qdrant_backend_and_url(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "vector_backend", "sqlite-vec")
    body = (await client.post("/api/settings/test-qdrant")).json()
    assert body["ok"] is False and "not 'qdrant'" in body["errors"][0]

    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "")
    body = (await client.post("/api/settings/test-qdrant")).json()
    assert body == {"ok": False, "errors": ["QDRANT_URL is not set"], "url": None}


async def test_qdrant_probe_http_outcomes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant.test:6333/")
    monkeypatch.setattr(settings, "qdrant_api_key", "k3y")

    seen = _mock_upstream(monkeypatch, lambda r: httpx.Response(200, json={"version": "1.9"}))
    body = (await client.post("/api/settings/test-qdrant")).json()
    assert body == {
        "ok": True, "errors": [], "url": "http://qdrant.test:6333", "version": {"version": "1.9"},
    }
    assert str(seen[0].url) == "http://qdrant.test:6333/version"  # trailing slash stripped
    assert seen[0].headers["api-key"] == "k3y"

    _mock_upstream(monkeypatch, lambda r: httpx.Response(503, text="overloaded"))
    body = (await client.post("/api/settings/test-qdrant")).json()
    assert body["ok"] is False
    assert body["errors"] == ["HTTP 503: overloaded"]

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock_upstream(monkeypatch, refuse)
    body = (await client.post("/api/settings/test-qdrant")).json()
    assert body["ok"] is False and "refused" in body["errors"][0]


async def test_readeck_probe_unconfigured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "readeck_base_url", None)
    monkeypatch.setattr(settings, "readeck_token", None)
    body = (await client.post("/api/settings/test-readeck")).json()
    assert body["ok"] is False
    assert "not set" in body["errors"][0]


async def test_readeck_probe_roles_and_failures(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "readeck_base_url", "http://readeck.test/")
    monkeypatch.setattr(settings, "readeck_token", "tok")

    profile = {"user": {"username": "me"}, "provider": {"roles": ["bookmarks:write"]}}
    seen = _mock_upstream(monkeypatch, lambda r: httpx.Response(200, json=profile))
    body = (await client.post("/api/settings/test-readeck")).json()
    assert body["ok"] is True and body["user"] == "me" and body["errors"] == []
    assert str(seen[0].url) == "http://readeck.test/api/profile"
    assert seen[0].headers["authorization"].endswith("tok")

    read_only = {"user": {"username": "me"}, "provider": {"roles": ["bookmarks:read"]}}
    _mock_upstream(monkeypatch, lambda r: httpx.Response(200, json=read_only))
    body = (await client.post("/api/settings/test-readeck")).json()
    assert body["ok"] is False
    assert "bookmarks:write" in body["errors"][0]

    _mock_upstream(monkeypatch, lambda r: httpx.Response(401))
    body = (await client.post("/api/settings/test-readeck")).json()
    assert body == {"ok": False, "errors": ["HTTP 401"], "url": "http://readeck.test"}

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    _mock_upstream(monkeypatch, refuse)
    body = (await client.post("/api/settings/test-readeck")).json()
    assert body["ok"] is False and "no route" in body["errors"][0]


async def test_probes_and_report_are_admin_only(client: AsyncClient) -> None:
    await setup_admin(client)
    await client.post("/api/users", json={"username": "reader", "password": "readerpass1"})
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "reader", "password": "readerpass1"})
    for path in ("test-llm", "test-qdrant", "test-readeck"):
        assert (await client.post(f"/api/settings/{path}")).status_code == 403
    assert (await client.get("/api/settings/threshold-report")).status_code == 403


async def test_threshold_report_empty(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.get("/api/settings/threshold-report")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


async def test_patch_settings_reset_and_length_rules(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "tau_gray", settings.tau_gray)  # restore after test
    r = await client.patch("/api/settings", json={"values": {"tau_gray": 0.42}})
    assert "tau_gray" in r.json()["overridden"]
    assert settings.tau_gray == 0.42

    # Empty value removes the override
    r = await client.patch("/api/settings", json={"values": {"tau_gray": ""}})
    assert "tau_gray" not in r.json()["overridden"]

    r = await client.patch(
        "/api/settings", json={"values": {"summary_min_sentences": 9, "summary_max_sentences": 3}}
    )
    assert r.status_code == 400 and "must not exceed" in r.json()["detail"]
    r = await client.patch("/api/settings", json={"values": {"llm_context_tokens": 10}})
    assert r.status_code == 400 and "between" in r.json()["detail"]
