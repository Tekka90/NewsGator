"""APScheduler worker: polls due feeds every minute (staggered by due-time check).

A single tick job scanning `is_due(feed)` keeps scheduling trivial and avoids
re-registering jobs on every feed CRUD operation.
"""

from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.db import get_session
from app.models import Feed
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


def start_scheduler() -> None:
    scheduler.add_job(
        poll_due_feeds,
        trigger="interval",
        minutes=1,
        id="poll_due_feeds",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
