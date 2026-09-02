"""Article processing pipeline: summarize → embed (SPEC §4 stages).

Runs as a single-worker asyncio queue so the local LLM server is never flooded;
queue depth is exposed for the GUI (SPEC §7). Results persist immediately per
article — the pipeline is resumable (invariant 7).

Clustering (the `clustered` stage) lands in Milestone 4; articles park in
`embedded` state until then.
"""

import asyncio
import time

import anyio
from langdetect import LangDetectException, detect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.models import Article, Category
from app.services import activity, llm_client, prompts, usage
from app.services.vectorstore import get_vector_store

_queue: asyncio.Queue[int] = asyncio.Queue()
_worker_task: asyncio.Task[None] | None = None
_in_flight = 0  # articles dequeued and currently being processed by the worker


def queue_depth() -> int:
    """For the GUI 'N articles waiting for LLM' indicator (SPEC §7).

    Counts queued articles *plus* the one being processed — once the worker
    dequeues an article, qsize() alone reads 0 even though the LLM is busy.
    """
    return _queue.qsize() + _in_flight


def enqueue_article(article_id: int) -> None:
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
        _in_flight += 1
        activity.broadcast_queue(queue_depth())
        try:
            async for session in get_session():
                await process_article(session, article_id)
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
    """Full per-article processing: detect language, summarize, categorize, embed."""
    article = await session.get(Article, article_id)
    if article is None or article.processing_state != "fulltext":
        return

    summarized = await summarize_article(session, article)
    if not summarized:
        return  # LLM failed — article stays in 'fulltext', retried on next sweep
    await embed_article(session, article)
    from app.services.cluster import cluster_article  # avoid import cycle at module load

    await cluster_article(session, article_id)
    await session.commit()


async def summarize_article(session: AsyncSession, article: Article) -> bool:
    """Detect language → LLM summary + category in SUMMARY_LANGUAGE (persisted at once).

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

    await activity.emit(session, "llm", "summarize_start", {"article_id": article.id})
    try:
        system, user = prompts.summarize_article(article.title, text, list(taxonomy))
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


async def enqueue_backlog(session: AsyncSession) -> int:
    """Requeue articles stuck mid-pipeline (crash recovery, invariant 7)."""
    rows = await session.scalars(
        select(Article.id).where(Article.processing_state == "fulltext")
    )
    count = 0
    for article_id in rows:
        enqueue_article(article_id)
        count += 1
    return count


async def count_pending(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(Article.id)).where(Article.processing_state == "fulltext")
        )
        or 0
    )
