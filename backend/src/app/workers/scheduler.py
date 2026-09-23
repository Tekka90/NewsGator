"""APScheduler worker: feed polling, freeze sweep, nightly retention."""

from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.config import settings
from app.core.db import get_session
from app.models import Feed
from app.services import activity
from app.services.cluster import freeze_old_stories
from app.services.ingest import is_due, poll_feed
from app.services.retention import purge_old_data

scheduler = AsyncIOScheduler()


async def poll_due_feeds() -> None:
    async for session in get_session():
        now = datetime.now(UTC)
        # Mail feeds are populated by the IMAP sweep, never RSS-polled
        feeds = (
            await session.scalars(
                select(Feed).where(Feed.is_enabled, Feed.kind == "rss")
            )
        ).all()
        due = [f for f in feeds if is_due(f, now)]
        for feed in due:
            await poll_feed(session, feed)  # errors handled inside poll_feed
        break


async def mail_poll_sweep() -> None:
    """Poll every enabled per-user IMAP account for newsletters (SPEC §9)."""
    from app.services.mailnews import poll_all_accounts

    await poll_all_accounts()


async def reader_poll_sweep() -> None:
    """Poll every enabled third-party reader account (SPEC §9, Google Reader API)."""
    from app.services.readers.sync import poll_all_reader_accounts

    await poll_all_reader_accounts()


async def freeze_sweep() -> None:
    """Hourly: freeze stories past the freeze window + prune the activity ring buffer."""
    async for session in get_session():
        await freeze_old_stories(session)
        await activity.prune(session)
        await session.commit()
        break


async def llm_backlog_sweep() -> None:
    """Requeue unclustered articles stuck mid-pipeline (LLM failure, crash recovery).

    Self-healing counterpart to the startup requeue (invariant 7): sweeps articles
    in 'fetched', 'fulltext', 'summarized', or 'embedded' so they resume gracefully.
    Idempotent — `process_article` no-ops if an article is already 'clustered'.
    """
    from app.services.process import enqueue_backlog

    async for session in get_session():
        requeued = await enqueue_backlog(session)
        if requeued:
            await activity.emit(session, "llm", "backlog_sweep", {"requeued": requeued})
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
    scheduler.add_job(
        llm_backlog_sweep,
        trigger="interval",
        minutes=settings.backlog_sweep_minutes,
        id="llm_backlog_sweep",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        mail_poll_sweep,
        trigger="interval",
        minutes=settings.mail_poll_minutes,
        id="mail_poll_sweep",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reader_poll_sweep,
        trigger="interval",
        minutes=settings.reader_poll_minutes,
        id="reader_poll_sweep",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        retention_sweep,
        trigger="cron",
        hour=3,
        minute=17,
        id="retention_sweep",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


async def retention_sweep() -> None:
    """Nightly retention job (SPEC §9, RETENTION_DAYS)."""
    async for session in get_session():
        await purge_old_data(session)
        break


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
