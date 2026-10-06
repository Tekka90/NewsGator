"""Sync engine for third-party RSS reader API accounts (SPEC §9).

Orchestrates polling from remote reader APIs, ingesting into virtual feeds,
resolving canonical publisher URLs, attributing original sub-feed titles,
and synchronizing read states bidirectionally.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Article, Feed, ReaderAccount, Story, StoryState, UserFeed
from app.services import activity
from app.services.fulltext import fetch_full_text_batch
from app.services.process import enqueue_article
from app.services.readers.greader import GReaderClient, GReaderItem

logger = logging.getLogger(__name__)

POLL_LOCK_TIMEOUT_S = 1800.0  # 30 minutes
_polling_accounts: dict[int, float] = {}


def try_begin_poll(account_id: int) -> bool:
    """Mark an account as being polled; False when a poll is already running."""
    now = time.monotonic()
    started = _polling_accounts.get(account_id)
    if started is not None and (now - started) < POLL_LOCK_TIMEOUT_S:
        return False
    _polling_accounts[account_id] = now
    return True


def end_poll(account_id: int) -> None:
    _polling_accounts.pop(account_id, None)


async def ensure_virtual_feed(session: AsyncSession, account: ReaderAccount) -> Feed:
    """Ensure the single virtual Feed exists for this reader account."""
    if account.virtual_feed_id is not None:
        feed = await session.get(Feed, account.virtual_feed_id)
        if feed is not None:
            return feed

    pseudo_url = f"reader:{account.provider}:{account.id}"
    feed = await session.scalar(select(Feed).where(Feed.url == pseudo_url))
    if feed is None:
        title = account.title.strip() if account.title else ""
        if not title:
            title = f"{account.provider.capitalize()} ({account.username or 'Reader'})"
        feed = Feed(
            url=pseudo_url,
            kind="reader_api",
            title=title,
            is_enabled=account.is_enabled,
            poll_interval_min=30,
            fetch_fulltext=True,
        )
        session.add(feed)
        await session.flush()

    account.virtual_feed_id = feed.id
    uf = await session.scalar(
        select(UserFeed).where(
            UserFeed.user_id == account.user_id, UserFeed.feed_id == feed.id
        )
    )
    if uf is None:
        session.add(UserFeed(user_id=account.user_id, feed_id=feed.id))
    return feed


async def poll_reader_account(session: AsyncSession, account: ReaderAccount) -> dict[str, int]:
    """Poll a third-party reader account, ingest items, and sync read states."""
    if not try_begin_poll(account.id):
        return {"new_articles": 0, "read_synced": 0}

    try:
        async with asyncio.timeout(POLL_LOCK_TIMEOUT_S):
            return await _poll_reader_account_inner(session, account)
    finally:
        end_poll(account.id)


READER_PAGE_SIZE = 100
# Used only when a service reports no ingestion times and the checkpoint falls back to our own
# clock, which may differ from the service's.
CLOCK_SKEW_MARGIN_S = 3600


def _reader_cutoff(account: ReaderAccount) -> datetime | None:
    """Oldest publication date to import; no own window follows the global one."""
    days = settings.feed_backfill_days if account.backfill_days is None else account.backfill_days
    return datetime.now(UTC) - timedelta(days=days) if days > 0 else None


def _crawl_ts(item: GReaderItem) -> int | None:
    return int(item.crawled_at.timestamp()) if item.crawled_at is not None else None


async def establish_reader_checkpoint(client: GReaderClient, account: ReaderAccount) -> None:
    """Set the starting watermark without importing anything.

    Walks the unread stream newest-first until ``initial_import_count`` items were seen and
    records the oldest ingestion time among them. Older unread items are left behind; the
    normal incremental sync then imports everything from that point on.
    """
    started = int(datetime.now(UTC).timestamp())
    wanted = max(1, account.initial_import_count)
    seen = 0
    oldest: int | None = None
    continuation: str | None = None
    while seen < wanted:
        page, next_continuation = await client.fetch_stream(
            continuation=continuation,
            limit=min(READER_PAGE_SIZE, wanted - seen),
            exclude_read=True,
        )
        if not page:
            break
        for item in page:
            seen += 1
            ts = _crawl_ts(item)
            if ts is not None:
                oldest = ts if oldest is None else min(oldest, ts)
        if next_continuation is None or next_continuation == continuation:
            break
        continuation = next_continuation
    # No unread items (or no ingestion times): start from now, tolerating clock skew.
    account.sync_checkpoint = oldest if oldest is not None else started - CLOCK_SKEW_MARGIN_S
    account.sync_cursor = None


async def _unread_pages(
    client: GReaderClient, checkpoint: int
) -> AsyncIterator[list[GReaderItem]]:
    """Yield unread items oldest-first in pages, starting at ``checkpoint`` (inclusive)."""
    oldest_ts = checkpoint
    first = True
    while True:
        page, continuation = await client.fetch_stream(
            limit=READER_PAGE_SIZE, exclude_read=True, oldest_ts=oldest_ts, oldest_first=True
        )
        if not page:
            return
        stamps = [ts for ts in map(_crawl_ts, page) if ts is not None]
        if first and len(stamps) > 1 and stamps[0] > stamps[-1]:
            # The service ignored ``r=o`` and answers newest-first: collect everything and
            # replay it oldest-first so the checkpoint can still advance page by page.
            logger.warning("Reader service ignored oldest-first ordering; collecting all pages")
            everything = list(page)
            seen_tokens: set[str] = set()
            while continuation and continuation not in seen_tokens:
                seen_tokens.add(continuation)
                more, continuation = await client.fetch_stream(
                    continuation=continuation,
                    limit=READER_PAGE_SIZE,
                    exclude_read=True,
                    oldest_ts=oldest_ts,
                )
                everything.extend(more)
            everything.sort(key=lambda i: _crawl_ts(i) or 0)
            for start in range(0, len(everything), READER_PAGE_SIZE):
                yield everything[start : start + READER_PAGE_SIZE]
            return
        first = False
        yield page
        if not stamps:
            return  # cannot page without ingestion times
        newest = max(stamps)
        if newest > oldest_ts:
            oldest_ts = newest
        elif len(page) < READER_PAGE_SIZE:
            return  # only the boundary item again: caught up
        else:
            oldest_ts += 1  # a full page sharing one second; step past it (dedupe covers overlap)


async def _ingest_items(
    session: AsyncSession, account: ReaderAccount, feed: Feed, items: list[GReaderItem]
) -> tuple[int, int, list[int], list[int]]:
    """Store new items and apply inbound read state.

    Returns (new_articles, read_synced, fulltext_pending, llm_handoff).
    """
    new_articles = 0
    read_synced = 0
    fulltext_pending: list[int] = []
    llm_handoff: list[int] = []

    for item in items:
        # Check if article already exists on this feed by GUID or canonical URL
        existing = await session.scalar(
            select(Article).where(
                Article.feed_id == feed.id,
                or_(Article.guid == item.id, Article.url == item.url),
            )
        )

        if existing is not None:
            # Inbound read state sync for existing articles
            if item.is_read and existing.story_id is not None:
                story = await session.get(Story, existing.story_id)
                if story is not None:
                    state = await session.get(StoryState, (account.user_id, story.id))
                    if state is None:
                        state = StoryState(user_id=account.user_id, story_id=story.id)
                        session.add(state)

                    # Check how many member articles this story has
                    member_count = (
                        await session.scalar(
                            select(func.count(Article.id)).where(Article.story_id == story.id)
                        )
                    ) or 1

                    if member_count <= 1:
                        # Single-source story: mark completely read
                        if not state.is_read:
                            state.is_read = True
                            state.read_at_version = story.version
                            state.read_at = datetime.now(UTC)
                            read_synced += 1
                    else:
                        # Multi-source story: mark as "Updated" (read_at_version = version - 1)
                        # so the user knows one source was read while others remain
                        if not state.is_read or state.read_at_version >= story.version:
                            state.is_read = True
                            state.read_at_version = max(0, story.version - 1)
                            state.read_at = datetime.now(UTC)
                            read_synced += 1
            continue

        article = Article(
            feed_id=feed.id,
            guid=item.id,
            url=item.url,
            title=item.title,
            raw_content=item.raw_content,
            image_url=item.image_url,
            published_at=item.published_at,
            origin_feed_title=item.origin_feed_title,
            processing_state="fetched",
        )
        session.add(article)
        await session.flush()

        if feed.fetch_fulltext:
            fulltext_pending.append(article.id)
        else:
            article.processing_state = "fulltext"
            llm_handoff.append(article.id)

        new_articles += 1

    return new_articles, read_synced, fulltext_pending, llm_handoff


async def _process_page(feed_id: int, fulltext_pending: list[int], llm_handoff: list[int]) -> None:
    """Fulltext fetch and LLM handoff for one stored page, run while the next page loads."""
    ready = list(llm_handoff)
    if fulltext_pending:
        ready.extend(await fetch_full_text_batch(feed_id, fulltext_pending))
    for article_id in ready:
        enqueue_article(article_id)


async def _poll_reader_account_inner(
    session: AsyncSession, account: ReaderAccount
) -> dict[str, int]:
    feed = await ensure_virtual_feed(session, account)
    client = GReaderClient(
        api_base_url=account.api_base_url,
        username=account.username,
        password=account.password,
        auth_token=account.auth_token,
    )

    await activity.emit(
        session,
        "reader",
        "reader_poll_start",
        {
            "account_id": account.id,
            "provider": account.provider,
            "title": account.title or feed.title,
        },
    )
    await session.commit()

    new_articles = 0
    read_synced = 0
    background: list[asyncio.Task[None]] = []
    try:
        if account.sync_checkpoint is None:
            await establish_reader_checkpoint(client, account)
            await session.commit()
        checkpoint = account.sync_checkpoint or 0
        cutoff = _reader_cutoff(account)
        if cutoff is not None:
            checkpoint = max(checkpoint, int(cutoff.timestamp()))
        started = int(datetime.now(UTC).timestamp())
        async for page in _unread_pages(client, checkpoint):
            fresh = [
                item
                for item in page
                if cutoff is None or item.published_at is None or item.published_at >= cutoff
            ]
            new, synced, pending, handoff = await _ingest_items(session, account, feed, fresh)
            new_articles += new
            read_synced += synced
            stamps = [ts for ts in map(_crawl_ts, page) if ts is not None]
            # The checkpoint moves only with the page that was just stored.
            if stamps:
                account.sync_checkpoint = max(account.sync_checkpoint or 0, max(stamps))
            else:
                account.sync_checkpoint = max(
                    account.sync_checkpoint or 0, started - CLOCK_SKEW_MARGIN_S
                )
            if client.auth_token != account.auth_token:
                account.auth_token = client.auth_token
            await session.commit()
            background.append(asyncio.create_task(_process_page(feed.id, pending, handoff)))
    except Exception as exc:
        await session.rollback()
        account.last_error = str(exc)[:500]
        account.last_checked_at = datetime.now(UTC)
        await activity.emit(
            session,
            "reader",
            "reader_poll_failed",
            {"account_id": account.id, "error": account.last_error},
            level="error",
        )
        await session.commit()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        raise

    account.sync_cursor = None
    account.last_checked_at = datetime.now(UTC)
    account.last_error = None
    await activity.emit(
        session,
        "reader",
        "reader_poll_done",
        {
            "account_id": account.id,
            "provider": account.provider,
            "new_articles": new_articles,
            "read_synced": read_synced,
        },
    )
    await session.commit()
    if background:
        await asyncio.gather(*background)

    return {"new_articles": new_articles, "read_synced": read_synced}


async def sync_story_read_state_outbound(
    session: AsyncSession,
    user_id: int,
    story_id: int,
    is_read: bool,
) -> int:
    """Push read/unread state upstream to third-party reader APIs for member articles."""
    articles = (
        await session.scalars(
            select(Article).where(Article.story_id == story_id)
        )
    ).all()

    if not articles:
        return 0

    # Group articles by virtual feed_id
    by_feed: dict[int, list[Article]] = {}
    for a in articles:
        by_feed.setdefault(a.feed_id, []).append(a)

    pushed_count = 0
    for feed_id, feed_articles in by_feed.items():
        # Find if this feed belongs to a ReaderAccount owned by user_id
        account = await session.scalar(
            select(ReaderAccount).where(
                ReaderAccount.user_id == user_id,
                ReaderAccount.virtual_feed_id == feed_id,
                ReaderAccount.is_enabled.is_(True),
            )
        )
        if account is None:
            continue

        item_ids = [a.guid for a in feed_articles if a.guid]
        if not item_ids:
            continue

        client = GReaderClient(
            api_base_url=account.api_base_url,
            username=account.username,
            password=account.password,
            auth_token=account.auth_token,
        )

        try:
            await client.set_read_state(item_ids=item_ids, is_read=is_read)
            if client.auth_token != account.auth_token:
                account.auth_token = client.auth_token
                await session.commit()

            pushed_count += len(item_ids)
            await activity.emit(
                session,
                "reader",
                "reader_read_pushed",
                {
                    "account_id": account.id,
                    "story_id": story_id,
                    "items": len(item_ids),
                    "is_read": is_read,
                },
            )
            await session.commit()
        except Exception as exc:
            logger.warning(
                "Failed to push read state to reader account %s for story %s: %s",
                account.id,
                story_id,
                exc,
            )

    return pushed_count


async def poll_all_reader_accounts() -> None:
    """Scheduler sweep: poll every enabled reader account across all users."""
    from app.core.db import get_session

    async for session in get_session():
        accounts = (
            await session.scalars(
                select(ReaderAccount).where(ReaderAccount.is_enabled.is_(True))
            )
        ).all()
        for account in accounts:
            try:
                await poll_reader_account(session, account)
            except Exception as exc:
                logger.warning("Error polling reader account %s: %s", account.id, exc)
        break
