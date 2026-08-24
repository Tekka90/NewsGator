"""Milestone 7 tests: retention purge + threshold feedback report + stats."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import setup_admin

from app.core.config import settings
from app.models import (
    Article,
    ClusterDecision,
    Feed,
    OverridePair,
    Story,
    StoryRevision,
    StoryState,
)
from app.services import feedback, retention
from app.services.vectorstore import InMemoryVectorStore


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    s = InMemoryVectorStore()
    monkeypatch.setattr(retention, "get_vector_store", lambda session=None: s)
    return s


async def _seed_old_and_fresh(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """Return (old_story_id, fresh_story_id)."""
    old_date = datetime.now(UTC) - timedelta(days=settings.retention_days + 10)
    async with factory() as s:
        feed = Feed(url="https://r.example.com/rss")
        s.add(feed)
        await s.flush()
        old_story = Story(title="Old", summary="s", last_updated_at=old_date)
        fresh_story = Story(title="Fresh", summary="s")
        s.add_all([old_story, fresh_story])
        await s.flush()
        s.add_all(
            [
                Article(
                    feed_id=feed.id, guid="old", url="https://n/old",
                    story_id=old_story.id, fetched_at=old_date,
                ),
                Article(
                    feed_id=feed.id, guid="new", url="https://n/new",
                    story_id=fresh_story.id,
                ),
            ]
        )
        s.add(StoryRevision(story_id=old_story.id, version=1, summary="s"))
        s.add(StoryState(user_id=1, story_id=old_story.id, is_read=True, read_at_version=1))
        await s.commit()
        return old_story.id, fresh_story.id


async def test_retention_purges_old_keeps_fresh(db_session, store) -> None:
    old_id, fresh_id = await _seed_old_and_fresh(db_session)

    async with db_session() as s:
        report = await retention.purge_old_data(s)
        assert report["articles"] == 1
        assert report["stories"] == 1
        assert await s.get(Story, old_id) is None
        assert await s.get(Story, fresh_id) is not None
        assert await s.scalar(select(func.count(StoryRevision.id))) == 0
        assert await s.scalar(select(func.count(StoryState.story_id))) == 0


async def test_threshold_report_with_labels(db_session) -> None:
    async with db_session() as s:
        feed = Feed(url="https://f.example.com/rss")
        s.add(feed)
        await s.flush()
        # two articles with logged decision similarities
        a1 = Article(feed_id=feed.id, guid="1", url="https://n/1")
        a2 = Article(feed_id=feed.id, guid="2", url="https://n/2")
        a3 = Article(feed_id=feed.id, guid="3", url="https://n/3")
        s.add_all([a1, a2, a3])
        await s.flush()
        s.add_all(
            [
                ClusterDecision(article_id=a1.id, similarity=0.90, decision="attach"),
                ClusterDecision(article_id=a2.id, similarity=0.72, decision="attach_confirmed"),
                ClusterDecision(article_id=a3.id, similarity=0.66, decision="new"),
            ]
        )
        # user corrections: a1 same (correct), a2 same (good attach), a3 different (correct reject)
        story = Story(title="S", summary="s")
        s.add(story)
        await s.flush()
        s.add_all(
            [
                OverridePair(article_id=a1.id, story_id=story.id, label="same"),
                OverridePair(article_id=a2.id, story_id=story.id, label="same"),
                OverridePair(article_id=a3.id, story_id=story.id, label="different"),
            ]
        )
        await s.commit()

        report = await feedback.threshold_report(s)
        assert report["labeled_pairs"] == 3
        assert report["decisions_logged"] == 3
        assert report["suggested_tau_attach"] is not None
        # τ=0.72 region separates 0.90/0.72 (same) from 0.66 (different)
        best = next(c for c in report["candidates"] if c["tau"] == report["suggested_tau_attach"])
        assert best["f1"] >= 0.9


async def test_threshold_report_empty(db_session) -> None:
    async with db_session() as s:
        report = await feedback.threshold_report(s)
        assert report["labeled_pairs"] == 0
        assert report["suggested_tau_attach"] is None


async def test_stats_endpoint(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    r = await client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"feeds", "articles", "stories", "llm_queue_depth"}


async def test_threshold_report_endpoint(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    r = await client.get("/api/settings/threshold-report")
    assert r.status_code == 200
    assert "candidates" in r.json()
