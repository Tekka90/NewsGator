"""Links the user shares into NewsGator from other apps (SPEC §9, `kind == "saved"`).

Each user owns one pseudo-feed (`saved:{user_id}`) that is never polled; every shared URL becomes
an article in it and goes through the normal pipeline (full text → summary → embedding → cluster).
Every story that gets such an article is also flagged saved for the sharing user (and only
that user), so shared links never expire (retention skips saved stories).
"""

from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed, StoryState, User, UserFeed, utcnow
from app.services import activity, youtube
from app.services.ingest import canonicalize_url

SAVED_FEED_PREFIX = "saved:"
SAVED_FEED_TITLE = "Shared with NewsGator"
MAX_URL_LENGTH = 2048


def saved_feed_url(user_id: int) -> str:
    return f"{SAVED_FEED_PREFIX}{user_id}"


def saved_feed_owner(feed: Feed) -> int | None:
    """User id owning a saved-links pseudo-feed, else None."""
    if feed.kind != "saved" or not feed.url.startswith(SAVED_FEED_PREFIX):
        return None
    raw = feed.url.removeprefix(SAVED_FEED_PREFIX)
    return int(raw) if raw.isdigit() else None


async def flag_story_saved(session: AsyncSession, user_id: int, story_id: int) -> None:
    """Set `saved_at` like `PUT /stories/<id>/saved`: idempotent, read state untouched."""
    state = await session.get(StoryState, (user_id, story_id))
    if state is None:
        state = StoryState(user_id=user_id, story_id=story_id)
        session.add(state)
    if state.saved_at is None:
        state.saved_at = utcnow()


async def flag_shared_article_story(session: AsyncSession, article: Article) -> None:
    """Flag the article's story saved for the owner of its shared-links feed (no-op otherwise).

    Called whenever an article joins a story (clustering, manual move/merge)."""
    if article.story_id is None:
        return
    feed = await session.get(Feed, article.feed_id)
    owner = saved_feed_owner(feed) if feed is not None else None
    if owner is None or await session.get(User, owner) is None:
        return
    await flag_story_saved(session, owner, article.story_id)


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
        # already known (maybe from another feed): sharing it again still saves its story
        if existing.story_id is not None:
            await flag_story_saved(session, user.id, existing.story_id)
            await session.commit()
        return existing, True
    title = urlparse(url).hostname or url
    image_url: str | None = None
    channel: str | None = None
    video_id = youtube.parse_video_id(url)
    if video_id:
        # Title/channel/thumbnail up front so the video isn't titled "youtube.com" while it
        # processes. oEmbed failure is non-fatal.
        oembed = await youtube.fetch_oembed(url)
        if oembed is not None:
            title, channel = oembed.title[:1024], oembed.author_name or None
            image_url = oembed.thumbnail_url
        image_url = image_url or youtube.thumbnail_url(video_id)
    feed = await get_or_create_saved_feed(session, user)
    article = Article(
        feed_id=feed.id,
        guid=url,
        url=url,
        title=title,
        image_url=image_url,
        origin_feed_title=channel,
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
