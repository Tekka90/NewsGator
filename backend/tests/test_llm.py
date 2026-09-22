"""Milestone 3 tests: LLM client, prompts, processing pipeline, vector store."""

import asyncio
import itertools

import numpy as np
import pytest
from sqlalchemy import select
from tests.test_ingest import _make_feed

from app.core.config import settings
from app.models import SEED_CATEGORIES, ActivityEvent, Article, Category, CategorySuggestion
from app.services import cluster, llm_client, process, prompts
from app.services.vectorstore import InMemoryVectorStore, cosine_similarity

_feed_counter = itertools.count(1)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process queue and vector store are module-global; isolate per test."""
    monkeypatch.setattr(process, "_queue", asyncio.Queue())
    store = InMemoryVectorStore()
    monkeypatch.setattr(process, "get_vector_store", lambda session=None: store)
    monkeypatch.setattr(cluster, "get_vector_store", lambda session=None: store)


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


def test_prompt_requests_suggested_category(monkeypatch: pytest.MonkeyPatch) -> None:
    _system, user = prompts.summarize_article("T", "x", ["Tech", "World"])
    assert "suggested_category" in user


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

    async def fake_chat_json(system: str, user: str, model: str | None = None):
        if "headline" in user.lower():
            return {"headline": "Story headline"}, 5
        return {"summary": "A summary.", "category": "Tech"}, 42

    monkeypatch.setattr(process.llm_client, "chat_json", fake_chat_json)

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
        assert a.processing_state == "clustered"  # M4: pipeline runs through clustering
        assert a.language != ""
        assert a.story_id is not None

        actions = (
            await s.scalars(
                select(ActivityEvent.action)
            )
        ).all()
        assert "summarize_start" in actions
        assert "summarize_done" in actions
        assert "embed_done" in actions
        assert "cluster_new" in actions


async def test_process_article_records_category_suggestion(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """suggested_category outside the taxonomy is logged, not auto-applied."""
    _mock_llm(monkeypatch)

    async def fake_chat_json(system: str, user: str, model: str | None = None):
        if "headline" in user.lower():
            return {"headline": "Story headline"}, 5
        return {"summary": "A summary.", "category": "Tech", "suggested_category": "Apple"}, 42

    monkeypatch.setattr(process.llm_client, "chat_json", fake_chat_json)

    async with db_session() as s:
        if await s.scalar(select(Category.id).limit(1)) is None:
            s.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await s.commit()

    article = await _article_in_state(db_session, "fulltext")

    async with db_session() as s:
        await process.process_article(s, article.id)

    async with db_session() as s:
        rows = (await s.scalars(select(CategorySuggestion))).all()
        assert len(rows) == 1
        assert rows[0].article_id == article.id
        assert rows[0].raw_text == "Apple"
        assert rows[0].normalized_text == "apple"


async def test_process_article_skips_suggestion_matching_taxonomy(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suggestion that (loosely) matches an existing category isn't logged."""
    _mock_llm(monkeypatch)

    async def fake_chat_json(system: str, user: str, model: str | None = None):
        if "headline" in user.lower():
            return {"headline": "Story headline"}, 5
        return {"summary": "A summary.", "category": "Tech", "suggested_category": "tech."}, 42

    monkeypatch.setattr(process.llm_client, "chat_json", fake_chat_json)

    async with db_session() as s:
        if await s.scalar(select(Category.id).limit(1)) is None:
            s.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await s.commit()

    article = await _article_in_state(db_session, "fulltext")

    async with db_session() as s:
        await process.process_article(s, article.id)

    async with db_session() as s:
        rows = (await s.scalars(select(CategorySuggestion))).all()
        assert rows == []


async def test_process_article_skips_wrong_state(db_session) -> None:
    article = await _article_in_state(db_session, "clustered")
    async with db_session() as s:
        await process.process_article(s, article.id)  # no-op
        a = await s.get(Article, article.id)
        assert a is not None and a.processing_state == "clustered"


async def test_process_article_resumes_from_summarized(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An article in 'summarized' state skips summarize and proceeds to embed/cluster."""
    _mock_llm(monkeypatch)

    summarize_called = False

    async def fake_chat_json(system: str, user: str, model: str | None = None):
        nonlocal summarize_called
        if "headline" in user.lower():
            return {"headline": "Story headline"}, 5
        summarize_called = True
        return {"summary": "A summary.", "category": "Tech"}, 42

    monkeypatch.setattr(process.llm_client, "chat_json", fake_chat_json)

    async with db_session() as s:
        if await s.scalar(select(Category.id).limit(1)) is None:
            s.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await s.commit()

    article = await _article_in_state(db_session, "summarized")
    async with db_session() as s:
        # Pre-set summary and category as summarize_article would have
        a = await s.get(Article, article.id)
        assert a is not None
        a.summary = "Pre-existing summary"
        a.category = "Tech"
        a.language = "en"
        await s.commit()

    async with db_session() as s:
        await process.process_article(s, article.id)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.processing_state == "clustered"
        assert a.summary == "Pre-existing summary"
        assert not summarize_called


async def test_process_article_timeout_emits_error_and_preserves_state(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Article processing timeout logs an error activity and leaves state retryable."""
    async def slow_chat(*args, **kwargs):
        raise TimeoutError("LLM call timed out")

    monkeypatch.setattr(process.llm_client, "chat_json", slow_chat)

    async with db_session() as s:
        if await s.scalar(select(Category.id).limit(1)) is None:
            s.add_all([Category(name=n) for n in SEED_CATEGORIES])
            await s.commit()

    article = await _article_in_state(db_session, "fulltext")
    async with db_session() as s:
        await process.process_article(s, article.id)

    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.processing_state == "fulltext"
        actions = (
            await s.scalars(
                select(ActivityEvent.action).where(ActivityEvent.level == "error")
            )
        ).all()
        assert "process_timeout" in actions


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
    # Clear any leftover queue state
    while not process._queue.empty():
        process._queue.get_nowait()
    process._queued_ids.clear()

    stuck_fetched = await _article_in_state(db_session, "fetched")
    stuck_fulltext = await _article_in_state(db_session, "fulltext")
    stuck_summarized = await _article_in_state(db_session, "summarized")
    stuck_embedded = await _article_in_state(db_session, "embedded")
    await _article_in_state(db_session, "clustered")

    async with db_session() as s:
        n = await process.enqueue_backlog(s)
        pending = await process.count_pending(s)
    assert n == 4
    assert pending == 4
    assert process.queue_depth() == 4
    # drain
    drained = []
    while not process._queue.empty():
        drained.append(process._queue.get_nowait())
    process._queued_ids.clear()
    assert drained == [stuck_fetched.id, stuck_fulltext.id, stuck_summarized.id, stuck_embedded.id]


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
