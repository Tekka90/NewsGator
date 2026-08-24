"""Retention job (SPEC §9): purge data older than RETENTION_DAYS.

Cascades: articles, stories (now empty), story revisions, per-user read states,
and vectors. Runs nightly via the scheduler.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    ActivityEvent,
    Article,
    ClusterDecision,
    OverridePair,
    Story,
    StoryRevision,
    StoryState,
)
from app.services import activity
from app.services.vectorstore import get_vector_store


async def purge_old_data(session: AsyncSession) -> dict[str, int]:
    cutoff = datetime.now(UTC) - timedelta(days=settings.retention_days)
    store = get_vector_store(session)
    report: dict[str, int] = {}

    old_articles = (
        await session.scalars(select(Article.id).where(Article.fetched_at < cutoff))
    ).all()
    for article_id in old_articles:
        await store.delete_article(article_id)
    await session.execute(
        delete(ClusterDecision).where(ClusterDecision.article_id.in_(old_articles))
    )
    await session.execute(
        delete(OverridePair).where(OverridePair.article_id.in_(old_articles))
    )
    await session.execute(delete(Article).where(Article.id.in_(old_articles)))
    report["articles"] = len(old_articles)

    old_stories = (
        await session.scalars(
            select(Story).where(Story.last_updated_at < cutoff)
        )
    ).all()
    story_ids = [s.id for s in old_stories]
    for story_id in story_ids:
        await store.delete_story(story_id)
    if story_ids:
        await session.execute(
            delete(StoryRevision).where(StoryRevision.story_id.in_(story_ids))
        )
        await session.execute(
            delete(StoryState).where(StoryState.story_id.in_(story_ids))
        )
        await session.execute(delete(Story).where(Story.id.in_(story_ids)))
    report["stories"] = len(story_ids)

    # Activity log: drop entries older than retention (ring buffer also applies)
    res = await session.execute(delete(ActivityEvent).where(ActivityEvent.ts < cutoff))
    report["activity_events"] = getattr(res, "rowcount", 0) or 0

    await activity.emit(session, "retention", "purge_done", report)
    await session.commit()
    return report
