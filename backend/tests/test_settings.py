"""Settings API tests (Milestone 3)."""

from httpx import AsyncClient
from tests.conftest import setup_admin

from app.core.config import settings


async def test_settings_require_admin(client: AsyncClient) -> None:
    assert (await client.get("/api/settings")).status_code == 401


async def test_get_and_patch_settings(client: AsyncClient) -> None:
    await setup_admin(client)

    r = await client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["summary_language"] == "en"
    assert body["values"]["retention_days"] == 45
    assert "llm_queue_depth" in body

    r = await client.patch(
        "/api/settings", json={"values": {"tau_attach": 0.9, "retention_days": 30}}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["tau_attach"] == 0.9
    assert body["values"]["retention_days"] == 30
    assert "tau_attach" in body["overridden"]
    # Runtime override actually applied to live config
    assert settings.tau_attach == 0.9

    # Unknown key rejected
    r = await client.patch("/api/settings", json={"values": {"nope": 1}})
    assert r.status_code == 400

    # Invalid type rejected
    r = await client.patch("/api/settings", json={"values": {"retention_days": "abc"}})
    assert r.status_code == 400


async def test_test_llm_endpoint_mocked(client: AsyncClient, monkeypatch) -> None:
    await setup_admin(client)

    async def fake_probe():
        return {"chat": True, "embeddings": False, "errors": ["embed down"]}

    from app.services import llm_client

    monkeypatch.setattr(llm_client, "test_connection", fake_probe)
    r = await client.post("/api/settings/test-llm")
    assert r.status_code == 200
    body = r.json()
    assert body["chat"] is True and body["embeddings"] is False
    assert "llm_base_url" in body
