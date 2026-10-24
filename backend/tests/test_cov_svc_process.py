"""Coverage tests: process worker loop, queue accounting and pipeline edge branches."""

import asyncio

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import ActivityEvent, Article, Feed
from app.services import cluster, process
from app.services.vectorstore import InMemoryVectorStore


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    monkeypatch.setattr(process, "_queue", asyncio.Queue())
    monkeypatch.setattr(process, "_queued_ids", set())
    monkeypatch.setattr(process, "_worker_task", None)
    monkeypatch.setattr(process, "_in_flight", 0)
    store = InMemoryVectorStore()
    monkeypatch.setattr(process, "get_vector_store", lambda session=None: store)
    monkeypatch.setattr(cluster, "get_vector_store", lambda session=None: store)
    return store


async def _article(db_session, *, fulltext: bool = False, **kw) -> int:
    async with db_session() as s:
        feed = Feed(url="https://p.test/rss", fetch_fulltext=fulltext)
        s.add(feed)
        await s.flush()
        art = Article(feed_id=feed.id, guid="g", url="https://p.test/1", title="Title", **kw)
        s.add(art)
        await s.commit()
        return art.id


def test_enqueue_dedupes_and_queue_depth_counts_in_flight(monkeypatch) -> None:
    broadcasts: list[int] = []
    monkeypatch.setattr(process.activity, "broadcast_queue", broadcasts.append)
    process.enqueue_article(5)
    process.enqueue_article(5)
    process.enqueue_article(6)
    assert process.queue_depth() == 2 and broadcasts == [1, 2]
    monkeypatch.setattr(process, "_in_flight", 1)
    assert process.queue_depth() == 3


async def test_worker_processes_queue_and_survives_failures(db_session, monkeypatch) -> None:
    handled: list[int] = []

    async def fake_process(session, article_id: int) -> None:
        handled.append(article_id)
        if article_id == 2:
            raise RuntimeError("kaboom")
        if article_id == 3:
            raise TimeoutError

    monkeypatch.setattr(process, "process_article", fake_process)
    for i in (1, 2, 3, 4):
        process.enqueue_article(i)
    process.start_worker()
    first = process._worker_task
    process.start_worker()
    assert process._worker_task is first
    await asyncio.wait_for(process._queue.join(), 10)
    await process.stop_worker()
    assert process._worker_task is None
    await process.stop_worker()  # idempotent

    assert handled == [1, 2, 3, 4]  # a failure never stops the loop
    assert process._in_flight == 0 and process._queued_ids == set()
    async with db_session() as s:
        errs = (
            await s.scalars(
                select(ActivityEvent)
                .where(ActivityEvent.action == "process_error")
                .order_by(ActivityEvent.id)
            )
        ).all()
    assert len(errs) == 2 and all(e.level == "error" for e in errs)
    assert "RuntimeError: kaboom" in errs[0].detail
    assert "timed out after" in errs[1].detail


async def test_process_article_missing_is_noop(db_session) -> None:
    async with db_session() as s:
        await process.process_article(s, 12345)


async def test_fetched_without_fulltext_uses_raw_content(db_session, monkeypatch) -> None:
    aid = await _article(db_session, raw_content="Raw body text", processing_state="fetched")

    async def stop_after(session, article) -> bool:
        return False  # stop pipeline right after the fulltext stage

    monkeypatch.setattr(process, "summarize_article", stop_after)
    async with db_session() as s:
        await process.process_article(s, aid)
        art = await s.get(Article, aid)
        assert art is not None
        assert art.full_text == "Raw body text" and art.processing_state == "fulltext"


async def test_fetched_with_fulltext_feed_delegates(db_session, monkeypatch) -> None:
    from app.services import fulltext

    aid = await _article(db_session, fulltext=True, processing_state="fetched")
    seen: list[int] = []

    async def fake_fetch(session, article, feed) -> None:
        seen.append(feed.id)
        article.processing_state = "fulltext"

    async def stop(session, article) -> bool:
        return False

    monkeypatch.setattr(fulltext, "fetch_full_text", fake_fetch)
    monkeypatch.setattr(process, "summarize_article", stop)
    async with db_session() as s:
        await process.process_article(s, aid)
    assert seen == [1]


async def test_full_pipeline_to_clustered(db_session, monkeypatch, _isolate) -> None:
    aid = await _article(db_session, raw_content="x", processing_state="summarized", summary="S")
    clustered: list[int] = []

    async def fake_embed(texts, **kw):
        return [[1.0, 0.0]]

    async def fake_cluster(session, article_id: int) -> None:
        clustered.append(article_id)

    monkeypatch.setattr(process.llm_client, "embed", fake_embed)
    monkeypatch.setattr(cluster, "cluster_article", fake_cluster)
    async with db_session() as s:
        await process.process_article(s, aid)
        art = await s.get(Article, aid)
        assert art is not None and art.processing_state == "embedded"
    assert clustered == [aid]
    assert await _isolate.get_article_vector(aid) == [1.0, 0.0]


async def test_summarize_without_text_skips_llm(db_session) -> None:
    aid = await _article(db_session, processing_state="fulltext")
    async with db_session() as s:
        art = await s.get(Article, aid)
        assert art is not None
        assert await process.summarize_article(s, art) is True
        assert art.processing_state == "summarized" and not art.summary


async def test_summarize_undetectable_language_falls_back_to_server_language(
    db_session, monkeypatch
) -> None:
    aid = await _article(db_session, full_text="1234 5678 !!!", processing_state="fulltext")
    seen_lang: list[str] = []

    async def fake_chat(system: str, user: str, **kw):
        return {"summary": "ok", "category": "Nonexistent"}, 3

    real = process.prompts.summarize_article

    def spy(title, text, taxonomy, *, lang_code, media_kind=None):
        seen_lang.append(lang_code)
        return real(title, text, taxonomy, lang_code=lang_code, media_kind=media_kind)

    def no_detect(text: str) -> str:
        raise process.LangDetectException(0, "no features")

    monkeypatch.setattr(process, "detect", no_detect)
    monkeypatch.setattr(process.prompts, "summarize_article", spy)
    monkeypatch.setattr(process.llm_client, "chat_json", fake_chat)
    monkeypatch.setattr(settings, "summary_language", "de")
    async with db_session() as s:
        art = await s.get(Article, aid)
        assert art is not None
        assert await process.summarize_article(s, art) is True
        assert art.language == "" and set(seen_lang) == {"de"}
        assert art.category == "Uncategorized"  # not in the taxonomy -> safe default
        assert art.summary == "ok"


async def test_embed_without_summary_skips_llm(db_session, monkeypatch) -> None:
    async def never(texts, **kw):
        raise AssertionError("must not embed an empty summary")

    monkeypatch.setattr(process.llm_client, "embed", never)
    aid = await _article(db_session, processing_state="summarized")
    async with db_session() as s:
        art = await s.get(Article, aid)
        assert art is not None
        await process.embed_article(s, art)
        assert art.processing_state == "embedded"


async def test_count_pending_matches_backlog(db_session, monkeypatch) -> None:
    async with db_session() as s:
        feed = Feed(url="https://c.test/rss")
        s.add(feed)
        await s.flush()
        for i, state in enumerate(["fetched", "summarized", "clustered", "embedded"]):
            s.add(
                Article(
                    feed_id=feed.id, guid=str(i), url=f"https://c.test/{i}", processing_state=state
                )
            )
        await s.commit()
        assert await process.count_pending(s) == 3
        assert await process.enqueue_backlog(s) == 3
    assert process.queue_depth() == 3
