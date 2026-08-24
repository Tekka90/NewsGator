"""Feeds CRUD tests."""

from httpx import AsyncClient
from tests.conftest import setup_admin

FEED_URL = "https://example.com/feed.xml"


async def test_feeds_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/feeds")).status_code == 401


async def test_feed_crud(client: AsyncClient) -> None:
    await setup_admin(client)

    r = await client.post("/api/feeds", json={"url": FEED_URL, "title": "Example"})
    assert r.status_code == 201, r.text
    feed = r.json()
    assert feed["title"] == "Example"
    assert feed["is_enabled"] is True

    # duplicate rejected
    r = await client.post("/api/feeds", json={"url": FEED_URL})
    assert r.status_code == 409

    r = await client.get("/api/feeds")
    assert len(r.json()) == 1

    r = await client.patch(f"/api/feeds/{feed['id']}", json={"poll_interval_min": 45})
    assert r.json()["poll_interval_min"] == 45

    # disable/enable
    r = await client.patch(f"/api/feeds/{feed['id']}", json={"is_enabled": False})
    assert r.json()["is_enabled"] is False
    r = await client.patch(f"/api/feeds/{feed['id']}", json={"is_enabled": True})
    body = r.json()
    assert body["is_enabled"] is True
    assert body["consecutive_failures"] == 0  # re-enable resets failure state

    r = await client.delete(f"/api/feeds/{feed['id']}")
    assert r.status_code == 204
    assert (await client.get("/api/feeds")).json() == []


async def test_feed_not_found(client: AsyncClient) -> None:
    await setup_admin(client)
    assert (await client.patch("/api/feeds/999", json={"title": "x"})).status_code == 404
    assert (await client.delete("/api/feeds/999")).status_code == 404


async def test_manual_refresh(client: AsyncClient, db_session, monkeypatch) -> None:
    """Force-refresh endpoints bypass the schedule (SPEC §6)."""
    from tests.test_ingest import RSS, _ok_http

    from app.services import ingest

    monkeypatch.setattr(ingest, "_http_get", _ok_http(RSS))
    await setup_admin(client)
    r = await client.post("/api/feeds", json={"url": "https://example.com/feed", "title": "T"})
    feed_id = r.json()["id"]

    # Per-feed refresh
    r = await client.post(f"/api/feeds/{feed_id}/refresh")
    assert r.status_code == 200
    assert r.json()["new_articles"] == 2

    # Idempotent on second call (dedupe)
    r = await client.post(f"/api/feeds/{feed_id}/refresh")
    assert r.json()["new_articles"] == 0

    # Refresh-all
    r = await client.post("/api/feeds/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["feeds_polled"] == 1

    # Disabled feed cannot be refreshed individually
    await client.patch(f"/api/feeds/{feed_id}", json={"is_enabled": False})
    assert (await client.post(f"/api/feeds/{feed_id}/refresh")).status_code == 400

    # Not found
    assert (await client.post("/api/feeds/999/refresh")).status_code == 404
