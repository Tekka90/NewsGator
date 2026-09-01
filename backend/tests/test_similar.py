"""Proximity-ranked candidate endpoints for the merge/move story pickers.

GET /api/stories/{id}/similar                     → merge candidates
GET /api/stories/articles/{id}/similar-stories    → move candidates
Both rank by exact cosine (ANN + re-rank), exclude self, and fall back to
unscored recency order when no query vector exists.
"""

import numpy as np
import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.api import stories as stories_api
from app.models import Article, Feed, Story
from app.services.vectorstore import InMemoryVectorStore


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    s = InMemoryVectorStore()
    monkeypatch.setattr(stories_api, "get_vector_store", lambda session=None: s)
    return s


def _vec(i: int, dim: int = 8) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[i % dim] = 1.0
    return v.tolist()


def _near(i: int, dim: int = 8) -> list[float]:
    """Cosine ~0.9999 to _vec(i)."""
    v = np.zeros(dim, dtype=np.float32)
    v[i % dim] = 0.99
    v[(i + 1) % dim] = 0.01
    return v.tolist()


async def _mk_story(s, title: str) -> Story:
    story = Story(title=title, summary="s")
    s.add(story)
    await s.flush()
    return story


async def test_similar_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/stories/1/similar")).status_code == 401
    assert (await client.get("/api/stories/articles/1/similar-stories")).status_code == 401


async def test_similar_not_found(client: AsyncClient, store: InMemoryVectorStore) -> None:
    await setup_admin(client)
    assert (await client.get("/api/stories/999/similar")).status_code == 404
    assert (await client.get("/api/stories/articles/999/similar-stories")).status_code == 404


async def test_similar_stories_ranked_by_cosine(
    client: AsyncClient, db_session, store: InMemoryVectorStore
) -> None:
    await setup_admin(client)
    async with db_session() as s:
        target = await _mk_story(s, "Target")
        close = await _mk_story(s, "Close match")
        far = await _mk_story(s, "Far match")
        await s.commit()
        ids = (target.id, close.id, far.id)
    await store.upsert_story_centroid(ids[0], _vec(0))
    await store.upsert_story_centroid(ids[1], _near(0))
    await store.upsert_story_centroid(ids[2], _vec(3))  # orthogonal

    r = await client.get(f"/api/stories/{ids[0]}/similar")
    assert r.status_code == 200, r.text
    body = r.json()
    # best-first, self excluded
    assert [c["id"] for c in body] == [ids[1], ids[2]]
    assert body[0]["title"] == "Close match"
    assert body[0]["similarity"] > 0.9
    assert body[1]["similarity"] < 0.1


async def test_similar_fallback_when_no_centroid(
    client: AsyncClient, db_session, store: InMemoryVectorStore
) -> None:
    """Story without a centroid → unscored recency fallback, still excludes self."""
    await setup_admin(client)
    async with db_session() as s:
        target = await _mk_story(s, "No centroid")
        other = await _mk_story(s, "Other")
        await s.commit()
        target_id, other_id = target.id, other.id
    # `other` gets a centroid, target doesn't → fallback path for the query
    await store.upsert_story_centroid(other_id, _vec(0))

    r = await client.get(f"/api/stories/{target_id}/similar")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["id"] for c in body] == [other_id]
    assert body[0]["similarity"] is None


async def test_article_similar_stories_ranked_and_excludes_own(
    client: AsyncClient, db_session, store: InMemoryVectorStore
) -> None:
    await setup_admin(client)
    async with db_session() as s:
        feed = Feed(url="https://sim.example.com/rss", title="Sim")
        s.add(feed)
        await s.flush()
        own = await _mk_story(s, "Own story")
        close = await _mk_story(s, "Close story")
        far = await _mk_story(s, "Far story")
        article = Article(
            feed_id=feed.id, guid="g1", url="https://n/1", story_id=own.id,
            processing_state="clustered",
        )
        s.add(article)
        await s.commit()
        article_id, own_id, close_id, far_id = article.id, own.id, close.id, far.id
    await store.upsert_article(article_id, _vec(0))
    await store.upsert_story_centroid(own_id, _vec(0))  # identical, but excluded
    await store.upsert_story_centroid(close_id, _near(0))
    await store.upsert_story_centroid(far_id, _vec(4))

    r = await client.get(f"/api/stories/articles/{article_id}/similar-stories")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["id"] for c in body] == [close_id, far_id]
    assert body[0]["similarity"] > body[1]["similarity"]


async def test_article_similar_fallback_when_no_vector(
    client: AsyncClient, db_session, store: InMemoryVectorStore
) -> None:
    """Article never embedded → unscored recency fallback excluding its story."""
    await setup_admin(client)
    async with db_session() as s:
        feed = Feed(url="https://sim2.example.com/rss", title="Sim2")
        s.add(feed)
        await s.flush()
        own = await _mk_story(s, "Own")
        other = await _mk_story(s, "Other")
        article = Article(feed_id=feed.id, guid="g2", url="https://n/2", story_id=own.id)
        s.add(article)
        await s.commit()
        article_id, own_id, other_id = article.id, own.id, other.id

    r = await client.get(f"/api/stories/articles/{article_id}/similar-stories")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["id"] for c in body] == [other_id]
    assert body[0]["similarity"] is None
    assert own_id not in [c["id"] for c in body]
