"""Article processing pipeline: summarize → embed (SPEC §4 stages).

Runs as a single-worker asyncio queue so the local LLM server is never flooded;
queue depth is exposed for the GUI (SPEC §7). Results persist immediately per
article — the pipeline is resumable (invariant 7).

Clustering (the `clustered` stage) lands in Milestone 4; articles park in
`embedded` state until then.
"""

import asyncio
import logging
import time

import anyio
from langdetect import LangDetectException, detect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.models import Article, Category, Feed
from app.services import activity, category_suggestions, llm_client, llmtrace, prompts, usage
from app.services.vectorstore import get_vector_store

logger = logging.getLogger(__name__)

_queue: asyncio.Queue[int] = asyncio.Queue()
_queued_ids: set[int] = set()
_worker_task: asyncio.Task[None] | None = None
_in_flight = 0  # articles dequeued and currently being processed by the worker


def queue_depth() -> int:
    """For the GUI 'N articles waiting for LLM' indicator (SPEC §7).

    Counts queued articles *plus* the one being processed — once the worker
    dequeues an article, qsize() alone reads 0 even though the LLM is busy.
    """
    return _queue.qsize() + _in_flight


def enqueue_article(article_id: int) -> None:
    if article_id in _queued_ids:
        return
    _queued_ids.add(article_id)
    _queue.put_nowait(article_id)
    activity.broadcast_queue(queue_depth())


def start_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run_worker())


async def stop_worker() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    _worker_task = None


async def _run_worker() -> None:
    global _in_flight
    while True:
        article_id = await _queue.get()
        _queued_ids.discard(article_id)
        _in_flight += 1
        activity.broadcast_queue(queue_depth())
        try:
            async for session in get_session():
                await process_article(session, article_id)
                break
        except TimeoutError:
            async for session in get_session():
                await activity.emit(
                    session,
                    "llm",
                    "process_error",
                    {
                        "article_id": article_id,
                        "error": (
                            f"Article processing timed out after "
                            f"{settings.article_process_timeout_minutes} minutes"
                        ),
                    },
                    level="error",
                )
                await session.commit()
                break
        except Exception as exc:
            async for session in get_session():
                await activity.emit(
                    session,
                    "llm",
                    "process_error",
                    {"article_id": article_id, "error": f"{type(exc).__name__}: {exc}"},
                    level="error",
                )
                await session.commit()
                break
        finally:
            _in_flight -= 1
            _queue.task_done()
            activity.broadcast_queue(queue_depth())


async def process_article(session: AsyncSession, article_id: int) -> None:
    """Full per-article processing: resume from current state through to clustered.

    Stages:
      fetched -> fulltext -> summarized -> embedded -> clustered
    """
    article = await session.get(Article, article_id)
    if article is None or article.processing_state == "clustered":
        return

    timeout_s = max(1.0, settings.article_process_timeout_minutes * 60.0)
    try:
        async with asyncio.timeout(timeout_s):
            # 1. fetched -> fulltext
            if article.processing_state == "fetched":
                feed = await session.get(Feed, article.feed_id)
                if feed is not None and feed.fetch_fulltext:
                    from app.services.fulltext import fetch_full_text

                    await fetch_full_text(session, article, feed)
                else:
                    article.full_text = article.raw_content
                    article.processing_state = "fulltext"
                    await session.commit()

            # 2. fulltext -> summarized
            if article.processing_state == "fulltext":
                summarized = await summarize_article(session, article)
                if not summarized:
                    return  # LLM failed — article stays in 'fulltext', retried on next sweep

            # 3. summarized -> embedded
            if article.processing_state == "summarized":
                await embed_article(session, article)

            # 4. embedded -> clustered
            if article.processing_state == "embedded":
                from app.services.cluster import cluster_article

                await cluster_article(session, article_id)
                await session.commit()
    except TimeoutError:
        logger.error(
            "Article %d processing timed out after %d minutes",
            article_id,
            settings.article_process_timeout_minutes,
        )
        await session.rollback()
        await activity.emit(
            session,
            "llm",
            "process_timeout",
            {
                "article_id": article_id,
                "timeout_minutes": settings.article_process_timeout_minutes,
            },
            level="error",
        )
        await session.commit()


async def summarize_article(session: AsyncSession, article: Article) -> bool:
    """Detect language → LLM summary in article's source language (fallback to SUMMARY_LANGUAGE).

    Returns True on success (state → 'summarized'). On LLM failure the article stays
    in 'fulltext' so the backlog sweep retries it, and False is returned.
    """
    text = article.full_text or article.raw_content
    if not text:
        article.processing_state = "summarized"  # nothing to do; don't block pipeline
        return True

    try:
        article.language = await anyio.to_thread.run_sync(detect, text)
    except LangDetectException:
        article.language = ""

    taxonomy = (await session.scalars(select(Category.name).order_by(Category.name))).all()
    article_lang = article.language if article.language else settings.summary_language

    await activity.emit(session, "llm", "summarize_start", {"article_id": article.id})
    try:
        system, user = prompts.summarize_article(
            article.title, text, list(taxonomy), lang_code=article_lang
        )
        with llmtrace.context("summarize", label=article.title, article_id=article.id):
            result, latency_ms = await llm_client.chat_json(system, user)
    except llm_client.LLMError as exc:
        await activity.emit(
            session,
            "llm",
            "summarize_error",
            {"article_id": article.id, "error": str(exc)},
            level="error",
        )
        # Leave processing_state at 'fulltext' — retryable on next sweep
        await session.commit()
        return False

    article.summary = str(result.get("summary", ""))
    category = str(result.get("category", ""))
    article.category = category if category in taxonomy else "Uncategorized"
    suggested = result.get("suggested_category")
    if suggested and settings.category_suggestions_enabled:
        await category_suggestions.record_suggestion(
            session, article.id, str(suggested), list(taxonomy)
        )
    article.processing_state = "summarized"
    usage.record(
        session,
        "summarize",
        endpoint="chat",
        model=settings.llm_model,
        latency_ms=latency_ms,
        article=article,
        prompt_chars=len(system) + len(user),
        completion_chars=len(article.summary),
    )
    await activity.emit(
        session,
        "llm",
        "summarize_done",
        {"article_id": article.id, "llm_ms": latency_ms},
    )
    await session.commit()
    return True


async def embed_article(session: AsyncSession, article: Article) -> None:
    """Embed `title + summary` (SUMMARY_LANGUAGE text) and store the vector."""
    if not article.summary:
        article.processing_state = "embedded"  # nothing meaningful to embed
        return
    text = f"{article.title}\n\n{article.summary}"
    start = time.monotonic()
    with llmtrace.context("embed", label=article.title, article_id=article.id):
        vectors = await llm_client.embed([text])
    latency_ms = int((time.monotonic() - start) * 1000)
    usage.record(
        session,
        "embed",
        endpoint="embed",
        model=settings.embed_model,
        latency_ms=latency_ms,
        article=article,
        prompt_chars=len(text),
    )
    store = get_vector_store(session)
    await store.upsert_article(article.id, vectors[0])
    article.processing_state = "embedded"
    await activity.emit(session, "llm", "embed_done", {"article_id": article.id})
    await session.commit()


UNCLUSTERED_STATES = ["fetched", "fulltext", "summarized", "embedded"]


async def enqueue_backlog(session: AsyncSession) -> int:
    """Requeue articles stuck mid-pipeline (crash recovery, invariant 7).

    Picks up any unclustered article ('fetched', 'fulltext', 'summarized', 'embedded').
    """
    rows = await session.scalars(
        select(Article.id)
        .where(Article.processing_state.in_(UNCLUSTERED_STATES))
        .order_by(Article.id)
    )
    count = 0
    for article_id in rows:
        enqueue_article(article_id)
        count += 1
    return count


async def count_pending(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(Article.id)).where(
                Article.processing_state.in_(UNCLUSTERED_STATES)
            )
        )
        or 0
    )
