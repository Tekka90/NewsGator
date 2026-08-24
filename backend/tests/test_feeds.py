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
