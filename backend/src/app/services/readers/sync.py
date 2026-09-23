"""Sync engine for third-party RSS reader API accounts (SPEC §9).

Orchestrates polling from remote reader APIs, ingesting into virtual feeds,
resolving canonical publisher URLs, attributing original sub-feed titles,
and synchronizing read states bidirectionally.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed, ReaderAccount, Story, StoryState, UserFeed
from app.services import activity
from app.services.fulltext import fetch_full_text_batch
from app.services.process import enqueue_article
from app.services.readers.greader import GReaderAuthError, GReaderClient, GReaderError

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


async def _poll_reader_account_inner(session: AsyncSession, account: ReaderAccount) -> dict[str, int]:
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

    try:
        items, new_continuation = await client.fetch_stream(
            continuation=account.sync_cursor,
            limit=50,
        )
        if client.auth_token != account.auth_token:
            account.auth_token = client.auth_token
    except Exception as exc:
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
        raise

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

        # New article to ingest
        article = Article(
            feed_id=feed.id,
            guid=item.id,
            url=item.url,
            title=item.title,
            raw_content=item.raw_content,
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

    if new_continuation:
        account.sync_cursor = new_continuation
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

    # Fulltext fetch & LLM handoff (executed in small per-article transactions)
    if fulltext_pending:
        ready_for_llm = await fetch_full_text_batch(session, fulltext_pending)
        llm_handoff.extend(ready_for_llm)

    for article_id in llm_handoff:
        enqueue_article(article_id)

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

