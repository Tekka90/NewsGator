"""APScheduler worker: polls due feeds every minute + nightly-ish freeze sweep."""

from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.db import get_session
from app.models import Feed
from app.services import activity
from app.services.cluster import freeze_old_stories
from app.services.ingest import is_due, poll_feed

scheduler = AsyncIOScheduler()


async def poll_due_feeds() -> None:
    async for session in get_session():
        now = datetime.now(UTC)
        feeds = (await session.scalars(select(Feed).where(Feed.is_enabled))).all()
        due = [f for f in feeds if is_due(f, now)]
        for feed in due:
            await poll_feed(session, feed)  # errors handled inside poll_feed
        break


async def freeze_sweep() -> None:
    """Hourly: freeze stories past the freeze window + prune the activity ring buffer."""
    async for session in get_session():
        await freeze_old_stories(session)
        await activity.prune(session)
        await session.commit()
        break


def start_scheduler() -> None:
    scheduler.add_job(
        poll_due_feeds,
        trigger="interval",
        minutes=1,
        id="poll_due_feeds",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        freeze_sweep,
        trigger="interval",
        hours=1,
        id="freeze_sweep",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
