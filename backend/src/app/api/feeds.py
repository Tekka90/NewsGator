"""Feeds CRUD + OPML import/export. Ingestion itself is in services.ingest."""

import anyio
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.schemas import (
    DiscoveredFeed,
    FeedDiscoveryIn,
    FeedDiscoveryOut,
    FeedIn,
    FeedOut,
    FeedPatch,
)
from app.core.db import get_session
from app.models import (
    Article,
    ClusterDecision,
    Feed,
    OverridePair,
    Story,
    StoryRevision,
    StoryState,
    StoryTranslation,
    User,
    UserFeed,
)
from app.services import activity, discovery
from app.services.ingest import parse_opml, poll_feed, poll_feeds_background, render_opml
from app.services.vectorstore import get_vector_store

router = APIRouter(prefix="/feeds", tags=["feeds"], dependencies=[Depends(current_user)])


def _fetch_feed_title(url: str) -> str:
    """Blocking feedparser call — must run via anyio.to_thread."""
    import feedparser

    parsed = feedparser.parse(url)
    return str(parsed.feed.get("title", ""))


@router.get("")
async def list_feeds(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[FeedOut]:
    subscribed_feed_ids = select(UserFeed.feed_id).where(UserFeed.user_id == user.id)
    feeds = (
        await session.scalars(
            select(Feed).where(Feed.id.in_(subscribed_feed_ids)).order_by(Feed.id)
        )
    ).all()
    # feed_id → story ids with at least one source article from that feed
    links: dict[int, set[int]] = {}
    for feed_id, story_id in (
        await session.execute(
            select(Article.feed_id, Article.story_id).where(
                Article.feed_id.in_(subscribed_feed_ids),
                Article.story_id.is_not(None),
            )
        )
    ).all():
        if story_id is not None:
            links.setdefault(feed_id, set()).add(story_id)
    read_ids = {
        s.story_id
        for s in (
            await session.scalars(
                select(StoryState).where(
                    StoryState.user_id == user.id, StoryState.is_read.is_(True)
                )
            )
        ).all()
    }
    out: list[FeedOut] = []
    for feed in feeds:
        story_ids = links.get(feed.id, set())
        item = FeedOut.model_validate(feed)
        item.story_count = len(story_ids)
        item.unread_story_count = len(story_ids - read_ids)
        out.append(item)
    return out


class RefreshResult(BaseModel):
    new_articles: int


@router.post("/refresh")
async def refresh_all(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    """Force-poll every enabled RSS feed subscribed by the current user."""
    subscribed_feed_ids = select(UserFeed.feed_id).where(UserFeed.user_id == user.id)
    feeds = (
        await session.scalars(
            select(Feed).where(
                Feed.is_enabled,
                Feed.kind == "rss",
                Feed.id.in_(subscribed_feed_ids),
            )
        )
    ).all()
    total = 0
    for feed in feeds:
        total += await poll_feed(session, feed)
    return {"feeds_polled": len(feeds), "new_articles": total}


@router.post("/discover")
async def discover_feeds(
    body: FeedDiscoveryIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FeedDiscoveryOut:
    """Discover candidate RSS/Atom feeds using multi-turn LLM research and live verification."""
    results = await discovery.discover_feeds(
        session,
        location=body.location,
        themes=body.themes,
        query=body.query,
        excluded_urls=body.excluded_urls,
        lang_code=user.summary_language,
    )
    return FeedDiscoveryOut(feeds=[DiscoveredFeed.model_validate(f) for f in results])


@router.post("/{feed_id}/refresh")
async def refresh_feed(
    feed_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> RefreshResult:
    """Force-poll one feed now."""
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    uf = await session.scalar(
        select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed_id)
    )
    if uf is None and not user.is_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    if not feed.is_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Feed is disabled")
    if feed.kind != "rss":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Newsletter feeds are refreshed by polling the mail account (Settings)",
        )
    return RefreshResult(new_articles=await poll_feed(session, feed))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_feed(
    body: FeedIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FeedOut:
    url = str(body.url)
    exists = await session.scalar(select(Feed).where(Feed.url == url))
    if exists is not None:
        uf = await session.scalar(
            select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == exists.id)
        )
        if uf is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Already subscribed to this feed")
        session.add(UserFeed(user_id=user.id, feed_id=exists.id))
        await session.commit()
        await session.refresh(exists)
        story_ids = set(
            (
                await session.scalars(
                    select(Article.story_id).where(
                        Article.feed_id == exists.id, Article.story_id.is_not(None)
                    )
                )
            ).all()
        )
        read_ids = set(
            (
                await session.scalars(
                    select(StoryState.story_id).where(
                        StoryState.user_id == user.id,
                        StoryState.story_id.in_(story_ids),
                        StoryState.is_read.is_(True),
                    )
                )
            ).all()
        )
        item = FeedOut.model_validate(exists)
        item.story_count = len(story_ids)
        item.unread_story_count = len(story_ids - read_ids)
        # Poll right away for the newly subscribed feed
        poll_feeds_background([exists.id])
        return item

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
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.commit()
    await session.refresh(feed)
    # First poll right away instead of waiting for the next scheduler tick
    poll_feeds_background([feed.id])
    return FeedOut.model_validate(feed)


@router.patch("/{feed_id}")
async def update_feed(
    feed_id: int,
    body: FeedPatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FeedOut:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    uf = await session.scalar(
        select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed_id)
    )
    if uf is None and not user.is_admin:
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
async def delete_feed(
    feed_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    uf = await session.scalar(
        select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed_id)
    )
    if uf is None and not user.is_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")
    if uf is not None:
        await session.delete(uf)
        await session.flush()
    remaining = await session.scalar(
        select(func.count()).select_from(UserFeed).where(UserFeed.feed_id == feed_id)
    )
    if remaining == 0:
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
            rem = await session.scalar(
                select(func.count()).select_from(Article).where(Article.story_id == story_id)
            )
            if not rem:
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
            await session.execute(
                delete(StoryTranslation).where(StoryTranslation.story_id.in_(empty_story_ids))
            )
            await session.execute(delete(Story).where(Story.id.in_(empty_story_ids)))
        await activity.emit(
            session,
            "feeds",
            "feed_deleted",
            {
                "feed_id": feed_id,
                "title": feed.title,
                "url": feed.url,
                "articles_deleted": len(article_ids),
                "stories_deleted": len(empty_story_ids),
            },
        )
    else:
        await activity.emit(
            session,
            "feeds",
            "feed_unsubscribed",
            {"feed_id": feed_id, "user_id": user.id, "remaining_subscribers": remaining},
        )
    await session.commit()


@router.get("/export-opml")
async def export_opml(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> Response:
    """Export RSS-kind feeds subscribed by current user as an OPML 2.0 subscription list."""
    subscribed_feed_ids = select(UserFeed.feed_id).where(UserFeed.user_id == user.id)
    feeds = (
        await session.scalars(
            select(Feed)
            .where(Feed.kind == "rss", Feed.id.in_(subscribed_feed_ids))
            .order_by(Feed.id)
        )
    ).all()
    xml = render_opml(list(feeds))
    return Response(
        content=xml,
        media_type="text/x-opml",
        headers={"Content-Disposition": 'attachment; filename="newsgator-subscriptions.opml"'},
    )


class OpmlResult(BaseModel):
    added: int
    skipped_existing: int
    invalid: int
    feeds: list[FeedOut]


@router.post("/import-opml", status_code=status.HTTP_201_CREATED)
async def import_opml(
    file: UploadFile,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OpmlResult:
    """Bulk-import feeds from an OPML subscription export for current user."""
    content = await file.read()
    try:
        entries = await anyio.to_thread.run_sync(parse_opml, content)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a valid OPML file") from None

    all_feeds = {f.url: f for f in (await session.scalars(select(Feed))).all()}
    user_feed_ids = set(
        (await session.scalars(select(UserFeed.feed_id).where(UserFeed.user_id == user.id))).all()
    )
    added: list[Feed] = []
    new_feed_ids: list[int] = []
    skipped = invalid = 0
    for title, url in entries:
        if not url.startswith(("http://", "https://")):
            invalid += 1
            continue
        if url in all_feeds:
            existing_feed = all_feeds[url]
            if existing_feed.id in user_feed_ids:
                skipped += 1
                continue
            session.add(UserFeed(user_id=user.id, feed_id=existing_feed.id))
            user_feed_ids.add(existing_feed.id)
            added.append(existing_feed)
            new_feed_ids.append(existing_feed.id)
        else:
            feed = Feed(url=url, title=title)
            session.add(feed)
            await session.flush()
            all_feeds[url] = feed
            session.add(UserFeed(user_id=user.id, feed_id=feed.id))
            user_feed_ids.add(feed.id)
            added.append(feed)
            new_feed_ids.append(feed.id)
    await session.commit()
    for f in added:
        await session.refresh(f)
    if new_feed_ids:
        poll_feeds_background(new_feed_ids)
    return OpmlResult(
        added=len(added),
        skipped_existing=skipped,
        invalid=invalid,
        feeds=[FeedOut.model_validate(f) for f in added],
    )
