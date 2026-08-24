"""Milestone 3 tests: LLM client, prompts, processing pipeline, vector store."""

import asyncio
import itertools

import numpy as np
import pytest
from sqlalchemy import select
from tests.test_ingest import _make_feed

from app.core.config import settings
from app.models import SEED_CATEGORIES, ActivityEvent, Article, Category
from app.services import llm_client, process, prompts
from app.services.vectorstore import InMemoryVectorStore, cosine_similarity

_feed_counter = itertools.count(1)


@pytest.fixture(autouse=True)
def _fresh_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process queue is module-global; isolate it per test."""
    monkeypatch.setattr(process, "_queue", asyncio.Queue())


# --- LLM client ---


async def test_chat_json_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_msg = type("M", (), {"content": '{"summary": "hello"}'})()
    fake_choice = type("C", (), {"message": fake_msg})()
    fake_resp = type("R", (), {"choices": [fake_choice]})()

    class FakeCompletions:
        async def create(self, **kwargs):
            return fake_resp

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(llm_client, "_chat_client", lambda: FakeClient())
    result, latency = await llm_client.chat_json("sys", "user")
    assert result == {"summary": "hello"}
    assert latency >= 0


async def test_chat_json_retries_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(1)
            content = "not json" if len(calls) == 1 else '{"ok": true}'
            msg = type("M", (), {"content": content})()
            choice = type("C", (), {"message": msg})()
            return type("R", (), {"choices": [choice]})()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(llm_client, "_chat_client", lambda: FakeClient())
    result, _ = await llm_client.chat_json("sys", "user")
    assert result == {"ok": True}
    assert len(calls) == 2  # one retry


async def test_chat_json_fails_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCompletions:
        async def create(self, **kwargs):
            msg = type("M", (), {"content": "still not json"})()
            choice = type("C", (), {"message": msg})()
            return type("R", (), {"choices": [choice]})()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(llm_client, "_chat_client", lambda: FakeClient())
    with pytest.raises(llm_client.LLMError):
        await llm_client.chat_json("sys", "user")


async def test_embed_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEmbeddings:
        async def create(self, model: str, input: list[str]):
            data = [type("D", (), {"embedding": [0.1, 0.2]})() for _ in input]
            return type("R", (), {"data": data})()

    class FakeClient:
        embeddings = FakeEmbeddings()

    monkeypatch.setattr(llm_client, "_embed_client", lambda: FakeClient())
    out = await llm_client.embed(["a", "b"])
    assert len(out) == 2 and out[0] == [0.1, 0.2]


# --- prompts ---


def test_prompts_inject_language_and_taxonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "summary_language", "fr")
    system, user = prompts.summarize_article("Titre", "Texte", ["Tech", "Monde"])
    assert "French" in system + user
    assert "Tech" in user and "Monde" in user


def test_prompt_language_never_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for invariant 1: prompts must not hardcode English
    monkeypatch.setattr(settings, "summary_language", "de")
    system, user = prompts.summarize_article("T", "x", ["Tech"])
    assert "German" in system + user
    assert "English" not in system + user


# --- processing pipeline ---


async def _article_in_state(
    factory, state: str, full_text: str | None = "Some article body about tech news."
) -> Article:
    feed = await _make_feed(factory, url=f"https://news{next(_feed_counter)}.example.com/rss")
    async with factory() as s:
        article = Article(
            feed_id=feed.id,
            guid="g1",
            url="https://news.example.com/a",
            title="A title",
            raw_content="excerpt",
            full_text=full_text,
            processing_state=state,
        )
        s.add(article)
        await s.commit()
        return article


def _mock_llm(
    monkeypatch: pytest.MonkeyPatch, summary: str = "A summary.", category: str = "Tech"
) -> None:
    async def fake_chat_json(system: str, user: str, model: str | None = None):
        return {"summary": summary, "category": category}, 42

    async def fake_embed(texts: list[str], model: str | None = None):
        return [np.linspace(0, 1, 1024).tolist() for _ in texts]

    monkeypatch.setattr(process.llm_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(process.llm_client, "embed", fake_embed)


async def test_process_article_summarize_and_embed(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm(monkeypatch)
    store = InMemoryVectorStore()  # isolate vector store
    monkeypatch.setattr(process, "get_vector_store", lambda session=None: store)

    async with db_session() as s:
        if await s.scalar(select(Category.id).limit(1)) is None:
            s.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await s.commit()

    article = await _article_in_state(db_session, "fulltext")

    async with db_session() as s:
        await process.process_article(s, article.id)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.summary == "A summary."
        assert a.category == "Tech"
        assert a.processing_state == "embedded"
        assert a.language != ""

        actions = (
            await s.scalars(
                select(ActivityEvent.action).where(ActivityEvent.component == "llm")
            )
        ).all()
        assert "summarize_start" in actions
        assert "summarize_done" in actions
        assert "embed_done" in actions

    assert article.id in store.articles


async def test_process_article_skips_wrong_state(db_session) -> None:
    article = await _article_in_state(db_session, "fetched")
    async with db_session() as s:
        await process.process_article(s, article.id)  # no-op
        a = await s.get(Article, article.id)
        assert a is not None and a.processing_state == "fetched"


async def test_summarize_llm_error_leaves_retryable(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(system: str, user: str, model: str | None = None):
        raise llm_client.LLMError("down")

    monkeypatch.setattr(process.llm_client, "chat_json", boom)

    async def fake_embed(texts: list[str], model: str | None = None):
        return [np.linspace(0, 1, 1024).tolist() for _ in texts]

    monkeypatch.setattr(process.llm_client, "embed", fake_embed)
    store = InMemoryVectorStore()
    monkeypatch.setattr(process, "get_vector_store", lambda session=None: store)

    async with db_session() as s:
        if await s.scalar(select(Category.id).limit(1)) is None:
            s.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await s.commit()

    article = await _article_in_state(db_session, "fulltext")
    async with db_session() as s:
        await process.process_article(s, article.id)

    # Re-read in a fresh session to see the committed retryable state
    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.processing_state == "fulltext"  # retryable
        assert a.summary is None
        actions = (
            await s.scalars(
                select(ActivityEvent.action).where(ActivityEvent.level == "error")
            )
        ).all()
        assert "summarize_error" in actions


async def test_enqueue_backlog_recovers_stuck_articles(db_session) -> None:
    stuck = await _article_in_state(db_session, "fulltext")
    await _article_in_state(db_session, "embedded")
    # Fresh session: the helper's session is closed, so backlog sweep must see it
    async with db_session() as s:
        n = await process.enqueue_backlog(s)
    assert n == 1
    assert process.queue_depth() == 1
    # drain
    assert process._queue.get_nowait() == stuck.id


# --- vector store ---


async def test_inmemory_store_search() -> None:
    store = InMemoryVectorStore()
    a = [1.0, 0.0]
    b = [0.9, 0.1]
    c = [0.0, 1.0]
    await store.upsert_story_centroid(1, a)
    await store.upsert_story_centroid(2, c)
    results = await store.search_story_centroids(b, limit=2)
    assert results[0][0] == 1
    assert results[0][1] > results[1][1]


def test_cosine_similarity() -> None:
    assert cosine_similarity(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(1.0)
    assert cosine_similarity(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == pytest.approx(0.0)
    assert cosine_similarity(np.array([0.0, 0.0]), np.array([1.0, 0.0])) == 0.0
