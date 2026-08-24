"""Milestone 4 tests: clustering, story versioning, freeze, overrides, stories API."""

import itertools

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import setup_admin

from app.api import stories as stories_api
from app.core.config import settings
from app.models import (
    SEED_CATEGORIES,
    Article,
    Category,
    ClusterDecision,
    Feed,
    OverridePair,
    Story,
    StoryRevision,
)
from app.services import cluster, process
from app.services.vectorstore import InMemoryVectorStore

_counter = itertools.count(100)


def unit_vec(i: int, dim: int = 1024) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[i % dim] = 1.0
    return v.tolist()


def near_vec(i: int, dim: int = 1024) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[i % dim] = 0.99
    v[(i + 1) % dim] = 0.01
    return v.tolist()


def _vec_with_sim(i: int, similarity: float, dim: int = 1024) -> list[float]:
    """Unit vector with cosine similarity `similarity` to unit_vec(i)."""
    v = np.zeros(dim, dtype=np.float32)
    v[i % dim] = similarity
    v[(i + 1) % dim] = float(np.sqrt(1 - similarity**2))
    return v.tolist()


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    s = InMemoryVectorStore()
    # Patch everywhere get_vector_store is referenced (module-level imports bind names)
    monkeypatch.setattr(cluster, "get_vector_store", lambda session=None: s)
    monkeypatch.setattr(stories_api, "get_vector_store", lambda session=None: s)
    monkeypatch.setattr(process, "get_vector_store", lambda session=None: s)
    return s


async def _embedded_article(
    factory: async_sessionmaker[AsyncSession],
    summary: str,
    title: str = "An article",
) -> Article:
    n = next(_counter)
    async with factory() as s:
        feed = Feed(url=f"https://f{n}.example.com/rss")
        s.add(feed)
        await s.flush()
        article = Article(
            feed_id=feed.id,
            guid=f"g{n}",
            url=f"https://news.example.com/a{n}",
            title=title,
            summary=summary,
            processing_state="embedded",
        )
        s.add(article)
        await s.commit()
        return article


def _mock_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vectors: dict[str, list[float]] | None = None,
    default_vec: list[float] | None = None,
    same_event: bool = True,
    new_facts: bool = True,
    merged_summary: str = "Merged summary.",
    headline: str = "A headline",
) -> None:
    """Mock LLM: embed returns per-text vectors keyed by exact text, else default."""
    vectors = vectors or {}
    default = default_vec or unit_vec(0)

    async def fake_embed(texts: list[str], model: str | None = None):
        return [vectors.get(t, default) for t in texts]

    async def fake_chat_json(system: str, user: str, model: str | None = None):
        lowered = user.lower()
        if "same specific news event" in lowered:
            return {"same_event": same_event}, 10
        if "new facts" in lowered:
            return {"new_facts": new_facts, "added": "x"}, 10
        if "merge the new information" in lowered:
            return {"summary": merged_summary}, 10
        if "headline" in lowered:
            return {"headline": headline}, 10
        return {}, 10

    monkeypatch.setattr(cluster.llm_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(cluster.llm_client, "embed", fake_embed)


async def test_new_article_creates_story(db_session, store, monkeypatch) -> None:
    _mock_llm(monkeypatch, default_vec=unit_vec(1))
    article = await _embedded_article(db_session, "Apple launched the iPhone 18.")

    async with db_session() as s:
        await cluster.cluster_article(s, article.id)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.processing_state == "clustered"
        story = await s.get(Story, a.story_id)
        assert story is not None
        assert story.title == "A headline"
        assert story.version == 1
        rev = await s.scalar(select(StoryRevision).where(StoryRevision.story_id == story.id))
        assert rev is not None and rev.version == 1
        assert story.id in store.centroids
        dec = await s.scalar(select(ClusterDecision))
        assert dec is not None and dec.decision == "new"


async def test_similar_article_attaches_and_bumps_version(
    db_session, store, monkeypatch
) -> None:
    vec = unit_vec(2)
    monkeypatch.setattr(settings, "tau_attach", 0.8)
    monkeypatch.setattr(settings, "tau_gray", 0.5)
    _mock_llm(monkeypatch, default_vec=vec, new_facts=True)

    a1 = await _embedded_article(db_session, "iPhone 18 launched today.")
    a2 = await _embedded_article(db_session, "Apple unveiled iPhone 18 with new chip.")

    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)
        await cluster.cluster_article(s, a2.id)
        a2r = await s.get(Article, a2.id)
        assert a2r is not None
        story = await s.get(Story, a2r.story_id)
        assert story is not None
        assert a1.id != a2.id
        assert (await s.get(Article, a1.id)).story_id == story.id  # same story
        assert story.version == 2  # new facts → bump
        assert story.summary == "Merged summary."
        revs = (
            await s.scalars(select(StoryRevision).where(StoryRevision.story_id == story.id))
        ).all()
        assert len(revs) == 2
        decs = (await s.scalars(select(ClusterDecision))).all()
        assert any(d.decision == "attach" for d in decs)


async def test_duplicate_info_does_not_bump_version(db_session, store, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tau_attach", 0.8)
    _mock_llm(monkeypatch, default_vec=unit_vec(3), new_facts=False)

    a1 = await _embedded_article(db_session, "Same facts.")
    a2 = await _embedded_article(db_session, "Same facts again.")

    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)
        await cluster.cluster_article(s, a2.id)
        story = await s.scalar(select(Story))
        assert story is not None
        assert story.version == 1  # no new facts → no bump (invariant 3)
        count = await s.scalar(
            select(StoryRevision).where(StoryRevision.story_id == story.id)
        )
        assert count is not None


async def test_gray_zone_llm_confirmation(db_session, store, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tau_attach", 0.9)
    monkeypatch.setattr(settings, "tau_gray", 0.5)
    # Similarity between these two vectors ≈ 0.75 → inside the gray zone
    _mock_llm(
        monkeypatch,
        vectors={
            "An article\n\nEvent X happened.": unit_vec(10),
            "An article\n\nEvent X confirmed by officials.": _vec_with_sim(10, 0.75),
        },
        same_event=True,
    )

    a1 = await _embedded_article(db_session, "Event X happened.")
    a2 = await _embedded_article(db_session, "Event X confirmed by officials.")

    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)
        await cluster.cluster_article(s, a2.id)
        dec = await s.scalar(
            select(ClusterDecision).where(
                ClusterDecision.decision == "attach_confirmed"
            )
        )
        assert dec is not None
        assert (await s.get(Article, a2.id)).story_id == (await s.get(Article, a1.id)).story_id


async def test_gray_zone_llm_rejects_creates_new_story(
    db_session, store, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "tau_attach", 0.9)
    monkeypatch.setattr(settings, "tau_gray", 0.5)
    _mock_llm(
        monkeypatch,
        vectors={
            "An article\n\nTopic A.": unit_vec(11),
            "An article\n\nTopic A adjacent.": _vec_with_sim(11, 0.75),
        },
        same_event=False,
    )

    a1 = await _embedded_article(db_session, "Topic A.")
    a2 = await _embedded_article(db_session, "Topic A adjacent.")

    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)
        await cluster.cluster_article(s, a2.id)
        stories = (await s.scalars(select(Story))).all()
        assert len(stories) == 2  # rejected → separate story


async def test_frozen_story_excluded(db_session, store, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tau_attach", 0.8)
    _mock_llm(monkeypatch, default_vec=unit_vec(6))

    a1 = await _embedded_article(db_session, "Old news.")
    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)
        story = await s.scalar(select(Story))
        assert story is not None
        story.is_frozen = True  # simulate freeze
        await s.commit()

    a2 = await _embedded_article(db_session, "Old news again, days later.")
    async with db_session() as s:
        await cluster.cluster_article(s, a2.id)
        stories = (await s.scalars(select(Story))).all()
        assert len(stories) == 2  # frozen story skipped → new story


async def test_freeze_old_stories(db_session, store, monkeypatch) -> None:
    from datetime import timedelta

    async def fake_embed(texts: list[str], model: str | None = None):
        return [unit_vec(0) for _ in texts]

    async def fake_chat_json(system: str, user: str, model: str | None = None):
        return {"headline": "h"}, 1

    monkeypatch.setattr(cluster.llm_client, "embed", fake_embed)
    monkeypatch.setattr(cluster.llm_client, "chat_json", fake_chat_json)
    article = await _embedded_article(db_session, "Something.")
    async with db_session() as s:
        await cluster.cluster_article(s, article.id)
        story = await s.scalar(select(Story))
        assert story is not None
        assert story.is_frozen is False
        story.first_seen_at = story.first_seen_at - timedelta(
            hours=settings.freeze_after_hours + 1
        )
        await s.commit()
        n = await cluster.freeze_old_stories(s)
        assert n == 1
        assert (await s.scalar(select(Story))).is_frozen is True  # type: ignore[union-attr]


# --- stories API ---


async def _setup_story(client: AsyncClient, db_session) -> int:
    await setup_admin(client)
    async with db_session() as s:
        s.add_all([Category(name=n) for n in SEED_CATEGORIES])
        feed = Feed(url="https://api.example.com/rss")
        s.add(feed)
        await s.flush()
        story = Story(title="Big Story", summary="Summary v1", version=2)
        s.add(story)
        await s.flush()
        s.add(
            Article(
                feed_id=feed.id,
                guid="a1",
                url="https://news.example.com/x",
                title="Article X",
                story_id=story.id,
                processing_state="clustered",
            )
        )
        s.add(StoryRevision(story_id=story.id, version=1, summary="Summary v0"))
        s.add(StoryRevision(story_id=story.id, version=2, summary="Summary v1"))
        await s.commit()
        return story.id


async def test_stories_list_and_read_state(client: AsyncClient, db_session) -> None:
    story_id = await _setup_story(client, db_session)

    r = await client.get("/api/stories")
    assert r.status_code == 200
    item = next(i for i in r.json() if i["id"] == story_id)
    assert item["is_read"] is False
    assert item["source_count"] == 1

    # mark read at version 2
    assert (await client.post(f"/api/stories/{story_id}/read")).status_code == 204
    item = next(i for i in (await client.get("/api/stories")).json() if i["id"] == story_id)
    assert item["is_read"] is True
    assert item["updated_since_read"] is False

    # bump story version → updated_since_read flips on (invariant 4)
    async with db_session() as s:
        story = await s.get(Story, story_id)
        assert story is not None
        story.version = 3
        await s.commit()
    item = next(i for i in (await client.get("/api/stories")).json() if i["id"] == story_id)
    assert item["updated_since_read"] is True

    # unread filter hides read story; updated filter shows it
    ids = [i["id"] for i in (await client.get("/api/stories?filter=unread")).json()]
    assert story_id not in ids
    ids = [i["id"] for i in (await client.get("/api/stories?filter=updated")).json()]
    assert story_id in ids


async def test_story_detail_and_diff(client: AsyncClient, db_session) -> None:
    story_id = await _setup_story(client, db_session)
    r = await client.get(f"/api/stories/{story_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Big Story"
    assert len(body["articles"]) == 1
    assert len(body["revisions"]) == 2

    r = await client.get(f"/api/stories/{story_id}/diff?from=1")
    assert r.status_code == 200
    changes = r.json()["changes"]
    assert len(changes) == 1 and changes[0]["version"] == 2


async def test_merge_logs_override_pairs(client: AsyncClient, db_session, store) -> None:
    story_id = await _setup_story(client, db_session)
    async with db_session() as s:
        other = Story(title="Other", summary="Other summary")
        s.add(other)
        await s.flush()
        feed = await s.scalar(select(Feed))
        s.add(
            Article(
                feed_id=feed.id, guid="a2", url="https://news.example.com/y",
                title="Y", story_id=other.id, processing_state="clustered",
            )
        )
        await s.commit()
        other_id = other.id

    r = await client.post(f"/api/stories/{story_id}/merge", json={"source_story_id": other_id})
    assert r.status_code == 204

    async with db_session() as s:
        assert await s.get(Story, other_id) is None
        pairs = (await s.scalars(select(OverridePair))).all()
        assert any(p.label == "same" and p.story_id == story_id for p in pairs)


async def test_move_article_logs_different_label(client: AsyncClient, db_session) -> None:
    story_id = await _setup_story(client, db_session)
    async with db_session() as s:
        target = Story(title="Target", summary="t")
        s.add(target)
        await s.commit()
        target_id = target.id
        article_id = (await s.scalar(select(Article.id)))  # the one from setup

    r = await client.post(
        f"/api/stories/articles/{article_id}/move", json={"story_id": target_id}
    )
    assert r.status_code == 204
    async with db_session() as s:
        labels = {(p.story_id, p.label) for p in (await s.scalars(select(OverridePair))).all()}
        assert (story_id, "different") in labels
        assert (target_id, "same") in labels
