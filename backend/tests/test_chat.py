"""Chatbot tests: retrieval-grounded answer, citations, no-match, errors, auth."""

import numpy as np
import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.core.config import settings as env_settings
from app.models import Article, Feed, Story
from app.services import chat
from app.services.vectorstore import InMemoryVectorStore


@pytest.fixture(autouse=True)
def _chat_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(env_settings, "chat_enabled", True)
    monkeypatch.setattr(env_settings, "summary_language", "en")


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    s = InMemoryVectorStore()
    # chat.py imported get_vector_store directly — patch it on the chat module.
    monkeypatch.setattr(chat, "get_vector_store", lambda session=None: s)
    return s


def _vec(i: int, dim: int = 8) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[i % dim] = 1.0
    return v.tolist()


async def _make_story(db_session) -> int:
    async with db_session() as s:
        feed = Feed(url="https://f.example.com/rss", title="Feed")
        s.add(feed)
        await s.flush()
        story = Story(
            title="Big Event",
            summary="Something happened.",
            category="tech",
        )
        s.add(story)
        await s.flush()
        s.add(
            Article(
                feed_id=feed.id,
                guid="a1",
                url="https://news.example.com/one",
                title="Article One",
                story_id=story.id,
                processing_state="clustered",
            )
        )
        await s.commit()
        return story.id


async def test_chat_requires_auth(client: AsyncClient) -> None:
    assert (await client.post("/api/chat", json={"question": "q"})).status_code == 401


async def test_chat_disabled_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(env_settings, "chat_enabled", False)
    r = await client.post("/api/chat", json={"question": "what's new?"})
    assert r.status_code == 404


async def test_chat_answer_with_citations(
    client: AsyncClient, db_session, store: InMemoryVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)
    await store.upsert_story_centroid(story_id, _vec(0))

    async def fake_embed(texts):
        return [_vec(0)]

    async def fake_answer(system, user):
        assert f"[Story {story_id}]" in user
        return {"answer": f"Something happened [Story {story_id}].",
                "story_ids": [story_id]}, 321

    monkeypatch.setattr(chat, "_embed_query", fake_embed)
    monkeypatch.setattr(chat, "_answer", fake_answer)

    r = await client.post("/api/chat", json={"question": "what happened?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"].startswith("Something happened")
    assert body["latency_ms"] == 321
    assert len(body["stories"]) == 1
    st = body["stories"][0]
    assert st["id"] == story_id
    assert st["cited"] is True
    assert st["similarity"] == pytest.approx(1.0, abs=1e-4)
    assert st["source_hosts"] == ["news.example.com"]


async def test_chat_no_matching_stories(
    client: AsyncClient, db_session, store: InMemoryVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await setup_admin(client)

    async def fake_embed(texts):
        return [_vec(1)]

    async def boom(*args, **kwargs):  # must not be called with no grounding
        raise AssertionError("LLM answer must not run when no stories retrieved")

    monkeypatch.setattr(chat, "_embed_query", fake_embed)
    monkeypatch.setattr(chat, "_answer", boom)

    r = await client.post("/api/chat", json={"question": "anything?"})
    assert r.status_code == 200
    body = r.json()
    assert "couldn't find" in body["answer"]
    assert body["stories"] == []


async def test_chat_llm_failure_returns_502(
    client: AsyncClient, db_session, store: InMemoryVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)
    await store.upsert_story_centroid(story_id, _vec(0))

    async def fake_embed(texts):
        return [_vec(0)]

    async def bad_answer(system, user):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(chat, "_embed_query", fake_embed)
    monkeypatch.setattr(chat, "_answer", bad_answer)

    r = await client.post("/api/chat", json={"question": "what happened?"})
    assert r.status_code == 502
    assert "LLM answer failed" in r.json()["detail"]


async def test_chat_embed_failure_returns_502(
    client: AsyncClient, db_session, store: InMemoryVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await setup_admin(client)

    async def fake_embed(texts):
        return []

    monkeypatch.setattr(chat, "_embed_query", fake_embed)

    r = await client.post("/api/chat", json={"question": "what happened?"})
    assert r.status_code == 502
    assert "Embedding failed" in r.json()["detail"]
