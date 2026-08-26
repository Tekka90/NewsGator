"""Feeds CRUD (admin) + OPML import. Ingestion itself is in services.ingest."""

import anyio
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_user
from app.api.schemas import FeedIn, FeedOut, FeedPatch
from app.core.db import get_session
from app.models import (
    Article,
    ClusterDecision,
    Feed,
    OverridePair,
    Story,
    StoryRevision,
    StoryState,
)
from app.services import activity
from app.services.ingest import parse_opml, poll_feed, poll_feeds_background
from app.services.vectorstore import get_vector_store

router = APIRouter(prefix="/feeds", tags=["feeds"], dependencies=[Depends(admin_user)])


def _fetch_feed_title(url: str) -> str:
    """Blocking feedparser call — must run via anyio.to_thread."""
    import feedparser

    parsed = feedparser.parse(url)
    return str(parsed.feed.get("title", ""))


@router.get("")
async def list_feeds(session: AsyncSession = Depends(get_session)) -> list[FeedOut]:
    rows = await session.scalars(select(Feed).order_by(Feed.id))
    return [FeedOut.model_validate(f) for f in rows]


class RefreshResult(BaseModel):
    new_articles: int


@router.post("/refresh")
async def refresh_all(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    """Force-poll every enabled feed now (ignores the adaptive schedule)."""
    feeds = (await session.scalars(select(Feed).where(Feed.is_enabled))).all()
    total = 0
    for feed in feeds:
        total += await poll_feed(session, feed)
    return {"feeds_polled": len(feeds), "new_articles": total}


@router.post("/{feed_id}/refresh")
async def refresh_feed(
    feed_id: int, session: AsyncSession = Depends(get_session)
) -> RefreshResult:
    """Force-poll one feed now."""
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    if not feed.is_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Feed is disabled")
    return RefreshResult(new_articles=await poll_feed(session, feed))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_feed(body: FeedIn, session: AsyncSession = Depends(get_session)) -> FeedOut:
    url = str(body.url)
    exists = await session.scalar(select(Feed).where(Feed.url == url))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Feed already exists")
    title = body.title
    if not title:
        try:
            title = await anyio.to_thread.run_sync(_fetch_feed_title, url)
        except Exception:
            title = ""  # ingestion will retry; title stays editable
    feed = Feed(
        url=url,
        title=title,
        poll_interval_min=body.poll_interval_min,
        auth_cookies=body.auth_cookies,
        fetch_fulltext=body.fetch_fulltext,
        backfill_days=body.backfill_days,
    )
    session.add(feed)
    await session.commit()
    await session.refresh(feed)
    # First poll right away instead of waiting for the next scheduler tick
    poll_feeds_background([feed.id])
    return FeedOut.model_validate(feed)


@router.patch("/{feed_id}")
async def update_feed(
    feed_id: int, body: FeedPatch, session: AsyncSession = Depends(get_session)
) -> FeedOut:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(feed, field, value)
    # Manual re-enable resets the failure policy state (SPEC §9)
    if body.is_enabled is True:
        feed.consecutive_failures = 0
        feed.first_failure_at = None
        feed.last_error = None
    await session.commit()
    await session.refresh(feed)
    return FeedOut.model_validate(feed)


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feed(feed_id: int, session: AsyncSession = Depends(get_session)) -> None:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    # Bulk cascade by hand: the ORM relationship has no delete cascade, and
    # session.delete(feed) would try to lazy-load feed.articles at flush time
    # (fails under AsyncSession) to null out their NOT NULL feed_id.
    store = get_vector_store(session)
    article_ids = (
        await session.scalars(select(Article.id).where(Article.feed_id == feed_id))
    ).all()
    story_ids: list[int] = [
        sid
        for sid in (
            await session.scalars(
                select(Article.story_id)
                .where(Article.feed_id == feed_id, Article.story_id.is_not(None))
                .distinct()
            )
        ).all()
        if sid is not None
    ]
    for article_id in article_ids:
        await store.delete_article(article_id)
    if article_ids:
        await session.execute(
            delete(ClusterDecision).where(ClusterDecision.article_id.in_(article_ids))
        )
        await session.execute(
            delete(OverridePair).where(OverridePair.article_id.in_(article_ids))
        )
        await session.execute(delete(Article).where(Article.id.in_(article_ids)))
    await session.execute(delete(Feed).where(Feed.id == feed_id))
    # Stories left without any article become ghosts — purge them like retention does.
    empty_story_ids: list[int] = []
    for story_id in story_ids:
        remaining = await session.scalar(
            select(func.count()).select_from(Article).where(Article.story_id == story_id)
        )
        if not remaining:
            empty_story_ids.append(story_id)
    for story_id in empty_story_ids:
        await store.delete_story(story_id)
    if empty_story_ids:
        await session.execute(
            delete(StoryRevision).where(StoryRevision.story_id.in_(empty_story_ids))
        )
        await session.execute(
            delete(StoryState).where(StoryState.story_id.in_(empty_story_ids))
        )
        await session.execute(delete(Story).where(Story.id.in_(empty_story_ids)))
    await activity.emit(
        session,
        "feeds",
        "feed_deleted",
        {"feed_id": feed_id, "title": feed.title, "url": feed.url,
         "articles_deleted": len(article_ids),
         "stories_deleted": len(empty_story_ids)},
    )
    await session.commit()


class OpmlResult(BaseModel):
    added: int
    skipped_existing: int
    invalid: int
    feeds: list[FeedOut]


@router.post("/import-opml", status_code=status.HTTP_201_CREATED)
async def import_opml(
    file: UploadFile, session: AsyncSession = Depends(get_session)
) -> OpmlResult:
    """Bulk-import feeds from an OPML subscription export."""
    content = await file.read()
    try:
        entries = await anyio.to_thread.run_sync(parse_opml, content)
    except Exception:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Not a valid OPML file"
        ) from None

    existing = set((await session.scalars(select(Feed.url))).all())
    added: list[Feed] = []
    skipped = invalid = 0
    for title, url in entries:
        if not url.startswith(("http://", "https://")):
            invalid += 1
            continue
        if url in existing:
            skipped += 1
            continue
        feed = Feed(url=url, title=title)  # backfill_days None → server default
        session.add(feed)
        existing.add(url)
        added.append(feed)
    await session.commit()
    for f in added:
        await session.refresh(f)
    # First poll right away instead of waiting for the next scheduler tick
    poll_feeds_background([f.id for f in added])
    return OpmlResult(
        added=len(added),
        skipped_existing=skipped,
        invalid=invalid,
        feeds=[FeedOut.model_validate(f) for f in added],
    )
