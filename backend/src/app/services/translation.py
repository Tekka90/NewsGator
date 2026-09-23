"""Multi-language story translation service with pre-warming and caching.

Clustering and vector embeddings always remain in the global server language
(settings.summary_language). For users whose configured summary_language differs
from the server default, story headlines and summaries are translated via the LLM,
cached in `story_translation`, and pre-warmed in the background.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.models import Article, Story, StoryTranslation, User, UserFeed
from app.services import activity, llm_client, llmtrace, prompts, usage

logger = logging.getLogger(__name__)

# Queue for background pre-warming: (story_id, optional explicit target languages)
_queue: asyncio.Queue[tuple[int, list[str] | None]] = asyncio.Queue()
_worker_task: asyncio.Task[None] | None = None


def language_name(code: str) -> str:
    """Return the display language name for a given ISO code."""
    return prompts.LANGUAGE_NAMES.get(code, code)


async def _translate_llm(
    title: str, summary: str, target_lang_name: str
) -> tuple[dict[str, Any], int]:
    """Low-level LLM call seam (module-level for test monkeypatching)."""
    system, user = prompts.translate_story_text(title, summary, target_lang_name)
    with llmtrace.context(
        "story_translate", label=f"{title[:80]} → {target_lang_name}"
    ):
        return await llm_client.chat_json(system, user)


async def translate_story(
    session: AsyncSession, story: Story, target_language: str
) -> tuple[str, str]:
    """Translate story title and summary into target_language and cache the result.

    Returns (title, summary). Falls back to original title/summary if translation fails.
    """
    if not target_language or target_language == settings.summary_language:
        return story.title, story.summary

    target_name = language_name(target_language)
    try:
        data, latency_ms = await _translate_llm(story.title, story.summary, target_name)
        raw_title = data.get("title")
        raw_summary = data.get("summary")
        if not isinstance(raw_title, str) or not raw_title.strip():
            logger.warning(
                "Translation for story %d into %s was incomplete",
                story.id,
                target_language,
            )
            return story.title, story.summary
        if not isinstance(raw_summary, str) or not raw_summary.strip():
            logger.warning(
                "Translation for story %d into %s was incomplete",
                story.id,
                target_language,
            )
            return story.title, story.summary

        title_clean = raw_title.strip()
        summary_clean = raw_summary.strip()

        # Update or create cached translation
        trans = await session.get(StoryTranslation, (story.id, target_language))
        if trans is None:
            trans = StoryTranslation(
                story_id=story.id,
                language=target_language,
                version=story.version,
                title=title_clean,
                summary=summary_clean,
                created_at=datetime.now(UTC),
            )
            session.add(trans)
        else:
            trans.version = story.version
            trans.title = title_clean
            trans.summary = summary_clean
            trans.created_at = datetime.now(UTC)

        usage.record(
            session,
            "story_translate",
            endpoint="chat",
            model=settings.llm_model,
            latency_ms=latency_ms,
            story_id=story.id,
            prompt_chars=len(story.title) + len(story.summary),
            completion_chars=len(title_clean) + len(summary_clean),
        )

        await activity.emit(
            session,
            "translation",
            "story_translated",
            {"story_id": story.id, "language": target_language, "version": story.version},
        )
        await session.commit()
        return title_clean, summary_clean

    except Exception as exc:
        logger.warning(
            "Failed to translate story %d into %s: %s",
            story.id,
            target_language,
            exc,
        )
        return story.title, story.summary


async def get_or_translate_story(
    session: AsyncSession, story: Story, target_language: str
) -> tuple[str, str]:
    """Retrieve cached translation matching current story version, or translate JIT."""
    if not target_language or target_language == settings.summary_language:
        return story.title, story.summary

    trans = await session.get(StoryTranslation, (story.id, target_language))
    if trans is not None and trans.version == story.version:
        return trans.title, trans.summary

    return await translate_story(session, story, target_language)


async def batch_get_translations(
    session: AsyncSession, story_ids: Sequence[int], target_language: str
) -> dict[int, tuple[str, str, int]]:
    """Fetch cached translations for multiple stories.

    Returns a dict: {story_id: (title, summary, version)}.
    """
    if not story_ids or not target_language or target_language == settings.summary_language:
        return {}

    rows = (
        await session.scalars(
            select(StoryTranslation).where(
                StoryTranslation.story_id.in_(story_ids),
                StoryTranslation.language == target_language,
            )
        )
    ).all()
    return {t.story_id: (t.title, t.summary, t.version) for t in rows}


def enqueue_story_translation(
    story_id: int, languages: list[str] | None = None
) -> None:
    """Enqueue a story for background translation pre-warming."""
    _queue.put_nowait((story_id, languages))


async def prewarm_user_stories(
    session: AsyncSession, user_id: int, target_language: str, limit: int = 50
) -> None:
    """Enqueue recent stories subscribed by a user when their summary language changes."""
    if not target_language or target_language == settings.summary_language:
        return

    # Subscribed story IDs for this user
    user_feed_ids = (
        await session.scalars(select(UserFeed.feed_id).where(UserFeed.user_id == user_id))
    ).all()
    if not user_feed_ids:
        return

    story_ids = (
        await session.scalars(
            select(Article.story_id)
            .distinct()
            .where(
                Article.feed_id.in_(user_feed_ids),
                Article.story_id.is_not(None),
            )
            .order_by(Article.story_id.desc())
            .limit(limit)
        )
    ).all()

    for sid in story_ids:
        if sid is not None:
            enqueue_story_translation(sid, [target_language])


async def _run_translation_worker() -> None:
    """Background consumer for story pre-warming."""
    while True:
        try:
            story_id, explicit_languages = await _queue.get()
        except asyncio.CancelledError:
            break

        try:
            async for session in get_session():
                story = await session.get(Story, story_id)
                if story is None:
                    break

                target_langs: list[str]
                if explicit_languages is not None:
                    target_langs = [
                        lang
                        for lang in explicit_languages
                        if lang and lang != settings.summary_language
                    ]
                else:
                    # Find distinct summary_languages of users subscribed to feeds of this story
                    user_langs = (
                        await session.scalars(
                            select(User.summary_language)
                            .distinct()
                            .join(UserFeed, UserFeed.user_id == User.id)
                            .join(Article, Article.feed_id == UserFeed.feed_id)
                            .where(
                                Article.story_id == story_id,
                                User.summary_language != "",
                                User.summary_language != settings.summary_language,
                            )
                        )
                    ).all()
                    target_langs = list(set(user_langs))

                for lang in target_langs:
                    # Check if already translated at current version
                    trans = await session.get(StoryTranslation, (story_id, lang))
                    if trans is not None and trans.version == story.version:
                        continue
                    await translate_story(session, story, lang)
                break
        except Exception as exc:
            logger.error("Error in translation worker for story %d: %s", story_id, exc)
        finally:
            _queue.task_done()


def start_translation_worker() -> None:
    """Start the translation background worker."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run_translation_worker())


async def stop_translation_worker() -> None:
    """Stop the translation background worker."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    _worker_task = None
