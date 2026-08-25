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


async def test_env_set_key_wins_and_is_locked(client: AsyncClient, monkeypatch) -> None:
    """An env-provided setting: reported as its env value, listed in env_locked,
    DB overrides rejected, and never shadowed by a stored row."""
    await setup_admin(client)
    monkeypatch.setenv("TAU_ATTACH", "0.77")

    r = await client.get("/api/settings")
    body = r.json()
    assert body["values"]["tau_attach"] == 0.77  # env value, not the code default
    assert "tau_attach" in body["env_locked"]

    # Runtime override attempt rejected
    r = await client.patch("/api/settings", json={"values": {"tau_attach": 0.9}})
    assert r.status_code == 400

    # A pre-existing DB row must not shadow the env var
    from app.core.db import get_session
    from app.models import Setting

    async for session in get_session():
        session.add(Setting(key="tau_attach", value="0.5"))
        await session.commit()
        break
    from app.api.settings import _apply_overrides

    # The stored row is not applied while the env var is set (the API reports
    # the env value 0.77). The singleton may already be dirty from earlier
    # tests — capture it, apply, and confirm the row didn't change it.
    before = settings.tau_attach
    _apply_overrides({"tau_attach": "0.5"})
    assert settings.tau_attach == before
    r = await client.get("/api/settings")
    assert r.json()["values"]["tau_attach"] == 0.77


async def test_db_override_still_works_without_env(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.patch("/api/settings", json={"values": {"tau_gray": 0.55}})
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["tau_gray"] == 0.55
    assert "tau_gray" in body["overridden"]
    assert "tau_gray" not in body["env_locked"]
    assert settings.tau_gray == 0.55
