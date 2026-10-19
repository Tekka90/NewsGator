"""Links the user shares into NewsGator from other apps (SPEC §9, `kind == "saved"`).

Each user owns one pseudo-feed (`saved:{user_id}`) that is never polled; every shared URL becomes
an article in it and goes through the normal pipeline (full text → summary → embedding → cluster).
"""

from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed, User, UserFeed, utcnow
from app.services import activity
from app.services.ingest import canonicalize_url

SAVED_FEED_PREFIX = "saved:"
SAVED_FEED_TITLE = "Shared with NewsGator"
MAX_URL_LENGTH = 2048


def saved_feed_url(user_id: int) -> str:
    return f"{SAVED_FEED_PREFIX}{user_id}"


def validate_shared_url(raw: str) -> str:
    """Return a canonical http(s) URL, or raise 422."""
    url = raw.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > MAX_URL_LENGTH:
        raise HTTPException(422, "A valid http(s) URL is required")
    return canonicalize_url(url)


async def get_or_create_saved_feed(session: AsyncSession, user: User) -> Feed:
    pseudo_url = saved_feed_url(user.id)
    feed = await session.scalar(select(Feed).where(Feed.url == pseudo_url))
    if feed is None:
        feed = Feed(url=pseudo_url, kind="saved", title=SAVED_FEED_TITLE, fetch_fulltext=True)
        session.add(feed)
        await session.flush()
    subscribed = await session.scalar(
        select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed.id)
    )
    if subscribed is None:
        session.add(UserFeed(user_id=user.id, feed_id=feed.id))
        await session.flush()
    return feed


async def import_url(session: AsyncSession, user: User, raw_url: str) -> tuple[Article, bool]:
    """Create (or find) the article for a shared URL. Returns `(article, already_known)`."""
    url = validate_shared_url(raw_url)
    existing = await session.scalar(
        select(Article)
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .where(UserFeed.user_id == user.id, Article.url == url)
        .limit(1)
    )
    if existing is not None:
        return existing, True
    feed = await get_or_create_saved_feed(session, user)
    article = Article(
        feed_id=feed.id,
        guid=url,
        url=url,
        title=urlparse(url).hostname or url,
        published_at=utcnow(),
        processing_state="fetched",
    )
    session.add(article)
    await session.flush()
    await activity.emit(
        session, "ingest", "link_shared", {"article_id": article.id, "feed_id": feed.id, "url": url}
    )
    await session.commit()
    return article, False
