"""Feeds CRUD tests."""

import asyncio

import pytest
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


async def test_new_feed_polled_immediately(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a feed kicks an immediate background poll (no scheduler tick wait)."""
    from sqlalchemy import func, select
    from tests.test_ingest import RSS, _ok_http

    from app.core.config import settings
    from app.models import Article
    from app.services import ingest

    monkeypatch.setattr(ingest, "_http_get", _ok_http(RSS))
    monkeypatch.setattr(settings, "environment", "dev")  # enable background kicks
    await setup_admin(client)

    r = await client.post("/api/feeds", json={"url": "https://example.com/feed", "title": "T"})
    assert r.status_code == 201, r.text

    # Drain the background poll task (same event loop as the ASGI transport)
    tasks = list(ingest._background_tasks)
    assert tasks, "expected a background poll to have been kicked"
    await asyncio.gather(*tasks)

    async with db_session() as s:
        count = await s.scalar(select(func.count(Article.id)))
        assert count == 2


async def test_delete_feed_cascades(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a feed with articles must not 500: articles, vectors, cluster
    rows and now-empty stories are cleaned up (regression: session.delete(feed)
    lazy-loaded feed.articles under AsyncSession and failed at flush)."""
    from sqlalchemy import func, select

    from app.api import feeds as feeds_api
    from app.models import Article, Feed, Story, StoryRevision, StoryState
    from app.services.vectorstore import InMemoryVectorStore

    store = InMemoryVectorStore()
    monkeypatch.setattr(feeds_api, "get_vector_store", lambda session=None: store)
    await setup_admin(client)

    async with db_session() as s:
        feed = Feed(url="https://del.example.com/rss", title="Del")
        s.add(feed)
        await s.flush()
        story = Story(title="Ghost", summary="s")
        s.add(story)
        await s.flush()
        a1 = Article(feed_id=feed.id, guid="1", url="https://n/1", story_id=story.id)
        a2 = Article(feed_id=feed.id, guid="2", url="https://n/2")
        s.add_all([a1, a2])
        await s.flush()
        s.add(StoryRevision(story_id=story.id, version=1, summary="s"))
        s.add(StoryState(user_id=1, story_id=story.id, is_read=True, read_at_version=1))
        await s.commit()
        feed_id, story_id = feed.id, story.id
        await store.upsert_article(a1.id, [0.1, 0.2])
        await store.upsert_article(a2.id, [0.1, 0.2])
        await store.upsert_story_centroid(story_id, [0.1, 0.2])

    r = await client.delete(f"/api/feeds/{feed_id}")
    assert r.status_code == 204, r.text

    async with db_session() as s:
        assert await s.scalar(select(func.count(Article.id))) == 0
        # Story lost all its articles → purged with its revision/state
        assert await s.get(Story, story_id) is None
        assert await s.scalar(select(func.count(StoryRevision.id))) == 0
        assert await s.scalar(select(func.count(StoryState.story_id))) == 0
