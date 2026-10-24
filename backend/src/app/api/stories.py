"""Stories API (SPEC §6): list with per-user flags, detail, read/unread, diff,
manual merge/move (logged as labeled pairs — invariant 9)."""

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Select, and_, case, delete, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import current_user
from app.core.config import settings
from app.core.db import get_session
from app.models import (
    Article,
    ArticleTranslation,
    Feed,
    ListenPosition,
    OverridePair,
    Story,
    StoryRevision,
    StoryState,
    StoryTranslation,
    User,
    UserFeed,
    utcnow,
)
from app.services import activity, listen_text, llm_client, process, saved_links
from app.services import story_presets as presets_svc
from app.services.fulltext import fetch_full_text
from app.services.vectorstore import cosine_similarity, get_vector_store

router = APIRouter(prefix="/stories", tags=["stories"])


class StoryListItem(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    image_url: str | None
    version: int
    is_frozen: bool
    source_count: int
    # distinct hosts of member article URLs (≤5) — for source favicons in the GUI
    source_hosts: list[str]
    # URL of the link the user shared into NewsGator (saved feed), if the story has one
    shared_link_url: str | None = None
    # earliest article publication date in the story (None if unknown)
    published_at: datetime | None
    last_updated_at: datetime
    is_read: bool
    updated_since_read: bool
    saved: bool
    saved_at: datetime | None
    readeck_bookmark_id: str | None
    # Video among the story's sources (persisted on the article, VIDEO_ARTICLES_PLAN)
    video_id: str | None = None  # YouTube videos only
    media_kind: str | None = None  # "video" or absent
    video_url: str | None = None
    video_embed_url: str | None = None
    video_provider: str | None = None  # youtube | file | embed
    video_duration: int | None = None  # seconds


class ArticleOut(BaseModel):
    id: int
    title: str
    url: str
    image_url: str | None
    language: str
    summary: str | None
    content_status: str
    content_warning: str | None
    published_at: datetime | None
    origin_feed_title: str | None = None
    feed_id: int
    feed_title: str
    feed_url: str
    video_id: str | None = None
    media_kind: str | None = None  # "video" when the article's main content is a video
    video_url: str | None = None
    video_embed_url: str | None = None
    video_provider: str | None = None
    video_duration: int | None = None
    has_full_text: bool = False  # Listen: full text available and usable once cleaned
    char_count: int = 0  # length of the cleaned text (0 if none)

    model_config = {"from_attributes": True}


class RevisionOut(BaseModel):
    version: int
    summary: str
    created_at: datetime

    model_config = {"from_attributes": True}


class StoryDetail(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    image_url: str | None
    version: int
    is_frozen: bool
    first_seen_at: datetime
    last_updated_at: datetime
    published_at: datetime | None
    is_read: bool
    updated_since_read: bool
    saved: bool
    saved_at: datetime | None
    articles: list[ArticleOut]
    revisions: list[RevisionOut]
    readeck_bookmark_id: str | None
    video_id: str | None = None
    media_kind: str | None = None
    video_url: str | None = None
    video_embed_url: str | None = None
    video_provider: str | None = None
    video_duration: int | None = None


def _article_out(a: Article) -> ArticleOut:
    has_full_text, char_count = listen_text.full_text_info(a)
    return ArticleOut(
        has_full_text=has_full_text,
        char_count=char_count,
        id=a.id,
        title=a.title,
        url=a.url,
        image_url=a.image_url,
        language=a.language,
        summary=a.summary,
        content_status=a.content_status,
        content_warning=a.content_warning,
        published_at=a.published_at,
        origin_feed_title=a.origin_feed_title,
        feed_id=a.feed_id,
        feed_title=a.feed.title,
        feed_url=a.feed.url,
        **_video_fields(a),
    )


def _video_fields(a: Article) -> dict[str, Any]:
    """Persisted video flag of an article, as API fields (absent/None for non-video)."""
    if a.media_kind != "video":
        return {}
    return {
        "media_kind": a.media_kind,
        "video_id": a.video_id,
        "video_url": a.video_url,
        "video_embed_url": a.video_embed_url,
        "video_provider": a.video_provider,
        "video_duration": a.video_duration,
    }


async def _story_videos(session: AsyncSession, story_ids: list[int]) -> dict[int, dict[str, Any]]:
    """story_id → video fields of the first (oldest) flagged member article."""
    videos: dict[int, dict[str, Any]] = {}
    for chunk in _chunked(story_ids):
        rows = await session.scalars(
            select(Article)
            .where(Article.story_id.in_(chunk), Article.media_kind == "video")
            .order_by(Article.id)
        )
        for article in rows.all():
            if article.story_id is not None and article.story_id not in videos:
                videos[article.story_id] = _video_fields(article)
    return videos


def _flags(state: StoryState | None, story: Story) -> tuple[bool, bool]:
    is_read = bool(state and state.is_read)
    updated = is_read and state is not None and story.version > state.read_at_version
    return is_read, updated


def _saved_flag(state: StoryState | None) -> tuple[bool, datetime | None]:
    saved_at = state.saved_at if state is not None else None
    return saved_at is not None, saved_at


async def _require_visible_story(session: AsyncSession, user: User, story_id: int) -> Story:
    story = await session.get(Story, story_id)
    if story is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
    visible = (
        await session.scalar(
            select(func.count())
            .select_from(Article)
            .join(UserFeed, UserFeed.feed_id == Article.feed_id)
            .where(Article.story_id == story_id, UserFeed.user_id == user.id)
        )
    ) or 0
    if not visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
    return story


def _source_hosts(rows: Iterable[tuple[int | None, str]]) -> dict[int, list[str]]:
    """story_id → distinct article hosts (≤5, first-seen order, www. stripped)."""
    from urllib.parse import urlparse

    hosts: dict[int, list[str]] = {}
    for story_id, url in rows:
        if story_id is None:
            continue
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        per_story = hosts.setdefault(story_id, [])
        if host and host not in per_story and len(per_story) < 5:
            per_story.append(host)
    return hosts


# SQLite caps bound parameters per statement; larger id sets are queried in chunks.
_ID_CHUNK = 500


async def _shared_links(session: AsyncSession, story_ids: list[int]) -> dict[int, str]:
    """story_id → URL of the article the user shared into NewsGator (feed kind ``saved``)."""
    links: dict[int, str] = {}
    for chunk in _chunked(story_ids):
        rows = await session.execute(
            select(Article.story_id, Article.url)
            .join(Feed, Feed.id == Article.feed_id)
            .where(Article.story_id.in_(chunk), Feed.kind == "saved")
        )
        for story_id, url in rows.all():
            if story_id is not None:
                links.setdefault(story_id, url)
    return links


def _chunked(ids: list[int]) -> Iterable[list[int]]:
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start : start + _ID_CHUNK]


async def _article_stats(
    session: AsyncSession, story_ids: list[int]
) -> dict[int, tuple[int, datetime | None]]:
    """story_id → (source article count, earliest publication date) for the given stories."""
    stats: dict[int, tuple[int, datetime | None]] = {}
    for chunk in _chunked(story_ids):
        rows = await session.execute(
            select(
                Article.story_id,
                func.count(Article.id),
                func.min(Article.published_at),
            )
            .where(Article.story_id.in_(chunk))
            .group_by(Article.story_id)
        )
        for story_id, count, first_published in rows.all():
            if story_id is not None:
                stats[story_id] = (count, first_published)
    return stats


async def _article_urls(
    session: AsyncSession, story_ids: list[int]
) -> list[tuple[int | None, str]]:
    rows: list[tuple[int | None, str]] = []
    for chunk in _chunked(story_ids):
        result = await session.execute(
            select(Article.story_id, Article.url).where(Article.story_id.in_(chunk))
        )
        rows.extend((story_id, url) for story_id, url in result.all())
    return rows


async def _user_feed_ids(session: AsyncSession, user: User) -> set[int]:
    return set(
        (await session.scalars(select(UserFeed.feed_id).where(UserFeed.user_id == user.id))).all()
    )


async def _story_items(
    session: AsyncSession,
    user: User,
    page: list[Story],
    states: dict[int, StoryState],
    stats: dict[int, tuple[int, datetime | None]],
) -> list[StoryListItem]:
    """Build list items (hosts, translations, flags) for the given stories."""
    page_ids = [s.id for s in page]

    article_urls = await _article_urls(session, page_ids)
    hosts = _source_hosts(article_urls)
    videos = await _story_videos(session, page_ids)
    shared = await _shared_links(session, page_ids)
    user_lang = user.summary_language or settings.summary_language
    from app.services.translation import batch_get_translations

    translations = await batch_get_translations(session, page_ids, user_lang)

    out: list[StoryListItem] = []
    for story in page:
        state = states.get(story.id)
        is_read, updated = _flags(state, story)
        is_saved, saved_at = _saved_flag(state)
        source_count, published_at = stats.get(story.id, (0, None))
        title = story.title
        summary = story.summary
        story_lang = story.language or settings.summary_language
        if user_lang != story_lang:
            t = translations.get(story.id)
            if t is not None and t[2] == story.version:
                title, summary = t[0], t[1]
            else:
                from app.services.translation import enqueue_story_translation

                enqueue_story_translation(story.id, [user_lang])
        out.append(
            StoryListItem(
                id=story.id,
                title=title,
                summary=summary,
                category=story.category,
                image_url=story.image_url,
                version=story.version,
                is_frozen=story.is_frozen,
                source_count=source_count,
                source_hosts=hosts.get(story.id, []),
                shared_link_url=shared.get(story.id),
                published_at=published_at,
                last_updated_at=story.last_updated_at,
                is_read=is_read,
                updated_since_read=updated,
                saved=is_saved,
                saved_at=saved_at,
                readeck_bookmark_id=story.readeck_bookmark_id,
                **videos.get(story.id, {}),
            )
        )
    return out


def _is_read_sql() -> ColumnElement[bool]:
    return func.coalesce(StoryState.is_read, false())


def _flag_conditions(filter: str, saved: bool) -> list[ColumnElement[bool]]:
    """SQL twin of ``_flags`` / ``_saved_flag`` (needs StoryState outer-joined)."""
    conditions: list[ColumnElement[bool]] = []
    if filter == "unread":
        conditions.append(_is_read_sql().is_(False))
    elif filter == "updated":
        conditions.append(
            and_(StoryState.is_read.is_(True), Story.version > StoryState.read_at_version)
        )
    if saved:
        conditions.append(StoryState.saved_at.is_not(None))
    return conditions


def _filtered_stories(
    user: User,
    feed_ids: set[int],
    *,
    filter: str,
    saved: bool,
    category: str | None,
    sort: str,
    reverse: bool,
    since: datetime | None,
    extra: Sequence[ColumnElement[bool]] = (),
) -> Select[Any]:
    """Visible stories of the user, filtered and ordered (published, sources, ...), in SQL.

    ``extra`` are additional AND conditions (preset criteria, feed sets); ``sort="saved_at"``
    orders by the user's save time (never-saved stories count as the smallest value)."""
    visible = select(Article.story_id).where(
        Article.feed_id.in_(feed_ids), Article.story_id.is_not(None)
    )
    stats = (
        select(
            Article.story_id.label("sid"),
            func.count(Article.id).label("n"),
            func.min(Article.published_at).label("first"),
        )
        .where(Article.story_id.is_not(None))
        .group_by(Article.story_id)
        .subquery()
    )
    query = (
        select(Story)
        .outerjoin(stats, stats.c.sid == Story.id)
        .outerjoin(StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id))
        .where(Story.id.in_(visible), *_flag_conditions(filter, saved), *extra)
    )
    if category:
        query = query.where(Story.category == category)
    recent_n = None
    if since is not None:
        recent = (
            select(Article.story_id.label("sid"), func.count(Article.id).label("n"))
            .where(
                Article.story_id.is_not(None),
                func.coalesce(Article.published_at, Article.fetched_at) >= since,
            )
            .group_by(Article.story_id)
            .subquery()
        )
        query = query.join(recent, recent.c.sid == Story.id)
        recent_n = recent.c.n

    def direction(column: Any) -> Any:
        return column.desc() if reverse else column.asc()

    if sort == "published":
        key = func.coalesce(stats.c.first, Story.last_updated_at)
        return query.order_by(direction(key), direction(Story.last_updated_at), direction(Story.id))
    if sort == "sources":
        count = recent_n if recent_n is not None else func.coalesce(stats.c.n, 0)
        return query.order_by(
            direction(count), direction(Story.last_updated_at), direction(Story.id)
        )
    if sort == "saved_at":
        return query.order_by(
            direction(StoryState.saved_at), direction(Story.last_updated_at), direction(Story.id)
        )
    return query.order_by(direction(Story.last_updated_at), direction(Story.id))


def _stories_of_feeds(feed_ids: Iterable[int]) -> ColumnElement[bool]:
    return Story.id.in_(
        select(Article.story_id).where(
            Article.feed_id.in_(list(feed_ids)), Article.story_id.is_not(None)
        )
    )


async def _feed_source_kinds(session: AsyncSession, feed_ids: set[int]) -> dict[int, str]:
    """Effective feed kind (rss|mail|youtube|saved|reader_api) of the given feeds."""
    rows = (
        await session.execute(select(Feed.id, Feed.kind, Feed.url).where(Feed.id.in_(feed_ids)))
    ).all()
    return {fid: presets_svc.effective_source_kind(kind, url) for fid, kind, url in rows}


def _stories_with_video(feed_ids: Iterable[int]) -> ColumnElement[bool]:
    """Stories with a flagged video article in one of the given feeds (indexed column)."""
    return Story.id.in_(
        select(Article.story_id).where(
            Article.feed_id.in_(list(feed_ids)),
            Article.story_id.is_not(None),
            Article.media_kind == "video",
        )
    )


def _criteria_conditions(
    criteria: dict[str, Any], feed_kinds: dict[int, str], now: datetime
) -> list[ColumnElement[bool]] | None:
    """SQL twin of ``story_presets.story_matches`` (needs StoryState outer-joined).

    None = the criteria can match nothing (e.g. a source kind none of the user's feeds has)."""
    status_ = criteria["status"]
    conditions = _flag_conditions(status_ if status_ in ("unread", "updated") else "all",
                                  status_ == "saved")
    if criteria["feed_ids"]:
        conditions.append(_stories_of_feeds(criteria["feed_ids"]))
    if criteria["category_ids"]:
        conditions.append(Story.category.in_(criteria["category_ids"]))
    if criteria["source_kinds"]:
        wanted = criteria["source_kinds"]
        of_kind = [f for f, k in feed_kinds.items() if k in wanted]
        by_kind: list[ColumnElement[bool]] = [_stories_of_feeds(of_kind)] if of_kind else []
        # `video` is not a feed kind: any flagged video article of the user's feeds
        if "video" in wanted and feed_kinds:
            by_kind.append(_stories_with_video(feed_kinds))
        if not by_kind:
            return None
        conditions.append(or_(*by_kind))
    if criteria["max_age_days"] is not None:
        conditions.append(Story.last_updated_at >= now - timedelta(days=criteria["max_age_days"]))
    haystack = func.ng_norm(func.coalesce(Story.title, "") + " " + func.coalesce(Story.summary, ""))
    for token in presets_svc.query_tokens(criteria["query"]):
        conditions.append(func.instr(haystack, token) > 0)
    return conditions


async def _page_items(session: AsyncSession, user: User, page: list[Story]) -> list[StoryListItem]:
    """List items for one page: statistics and read state are loaded for the page only."""
    page_ids = [s.id for s in page]
    stats = await _article_stats(session, page_ids)
    states: dict[int, StoryState] = {}
    for chunk in _chunked(page_ids):
        for state in (
            await session.scalars(
                select(StoryState).where(
                    StoryState.user_id == user.id, StoryState.story_id.in_(chunk)
                )
            )
        ).all():
            states[state.story_id] = state
    return await _story_items(session, user, page, states, stats)


@router.get("")
async def list_stories(
    filter: str = Query(default="all", pattern="^(all|unread|updated)$"),
    saved: bool = False,
    category: str | None = None,
    # only stories having at least one source article from this feed
    feed: int | None = None,
    # same, for several feeds (repeatable; OR within the list, AND with `feed`)
    feed_ids: list[int] = Query(default=[]),
    # apply a stored preset's criteria + sort + order; explicit sort/order replace the
    # preset's, feed/feed_ids/category/filter/saved/since are ANDed on top. 404 if the
    # preset is unknown or disabled.
    preset: str | None = None,
    sort: str | None = Query(default=None, pattern="^(updated|published|sources|saved_at)$"),
    order: str | None = Query(default=None, pattern="^(asc|desc)$"),
    # only stories with source articles published (else fetched) at or after this instant;
    # with sort=sources the ranking counts only those recent sources
    since: datetime | None = None,
    # optional paging, applied after filtering and sorting; omitted = the full list
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    # drop the read-status criterion (an `unread` filter/preset status becomes `all`) so
    # already-read stories stay reachable; every other criterion still applies
    include_read: bool = False,
    # cursor paging over the final ordering (needs `limit`): the `limit` stories right
    # before / after this story, in display order. Offsets shift as stories get read;
    # a story id does not. Empty when the story is not part of the list.
    before: int | None = Query(default=None, ge=1),
    after: int | None = Query(default=None, ge=1),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[StoryListItem]:
    if before is not None and after is not None:
        raise HTTPException(422, "Use either before or after")
    if (before is not None or after is not None) and limit is None:
        raise HTTPException(422, "Cursor paging needs a limit")
    if include_read and filter == "unread":
        filter = "all"
    user_feed_ids = await _user_feed_ids(session, user)
    stored: dict[str, Any] | None = None
    if preset is not None:
        presets = presets_svc.visible_presets(
            presets_svc.load_presets(user.story_presets), user_feed_ids
        )
        stored = next((p for p in presets if p["id"] == preset and p["enabled"]), None)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    sort = sort or (stored["sort"] if stored else "published")
    order = order or (stored["order"] if stored else "asc")
    if not user_feed_ids:
        return []
    if feed is not None and feed not in user_feed_ids:
        return []

    extra: list[ColumnElement[bool]] = []
    if stored is not None:
        criteria = stored["criteria"]
        if include_read and criteria["status"] == "unread":
            criteria = {**criteria, "status": "all"}
        kinds = (
            await _feed_source_kinds(session, user_feed_ids) if criteria["source_kinds"] else {}
        )
        conditions = _criteria_conditions(criteria, kinds, utcnow())
        if conditions is None:
            return []
        extra.extend(conditions)
    if feed_ids:
        visible_requested = [f for f in feed_ids if f in user_feed_ids]
        if not visible_requested:
            return []
        extra.append(_stories_of_feeds(visible_requested))

    query = _filtered_stories(
        user,
        {feed} if feed is not None else user_feed_ids,
        filter=filter,
        saved=saved,
        category=category,
        sort=sort,
        reverse=order == "desc",
        since=since,
        extra=extra,
    )
    cursor = before if before is not None else after
    if cursor is not None and limit is not None:
        ordered_ids = list((await session.scalars(query.with_only_columns(Story.id))).all())
        if cursor not in ordered_ids:
            return []
        at = ordered_ids.index(cursor)
        ids = ordered_ids[max(0, at - limit) : at] if before is not None else (
            ordered_ids[at + 1 : at + 1 + limit]
        )
        found = (await session.scalars(select(Story).where(Story.id.in_(ids)))).all()
        rows = {s.id: s for s in found}
        return await _page_items(session, user, [rows[i] for i in ids if i in rows])
    if limit is not None:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset) if limit is not None else query.limit(-1).offset(offset)
    page = list((await session.scalars(query)).all())
    return await _page_items(session, user, page)


class FeedOption(BaseModel):
    """A feed with at least one article in a story — story-list filter options."""

    id: int
    title: str
    kind: str
    url: str
    sender_email: str | None


# Declared before /{story_id} so the literal path wins over the int param.
class WidgetSnapshotOut(BaseModel):
    # unread stories visible to the user, before any limit
    unread_count: int
    # list name → stories; names: latest, latest_unread, oldest, oldest_unread,
    # most_sources, most_sources_unread
    lists: dict[str, list[StoryListItem]]


@router.get("/widget-snapshot")
async def widget_snapshot(
    # sources are counted over this trailing window for the most_sources lists
    since: datetime,
    limit: int = Query(default=12, ge=1, le=50),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> WidgetSnapshotOut:
    """Everything the home-screen widgets show, computed in one pass for the requesting user."""
    names = (
        "latest",
        "latest_unread",
        "oldest",
        "oldest_unread",
        "most_sources",
        "most_sources_unread",
    )
    empty = WidgetSnapshotOut(unread_count=0, lists={name: [] for name in names})
    user_feed_ids = await _user_feed_ids(session, user)
    if not user_feed_ids:
        return empty

    visible = select(Article.story_id).where(
        Article.feed_id.in_(user_feed_ids), Article.story_id.is_not(None)
    )
    # light columns only (no summaries); each aggregate runs once for all six lists
    rows = (
        await session.execute(
            select(
                Story.id,
                Story.last_updated_at,
                func.coalesce(StoryState.is_read, false()),
            )
            .outerjoin(
                StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id)
            )
            .where(Story.id.in_(visible))
        )
    ).all()
    ids = {row[0] for row in rows}
    first: dict[int, tuple[int, datetime | None]] = {}
    for story_id, count, earliest in (
        await session.execute(
            select(Article.story_id, func.count(Article.id), func.min(Article.published_at))
            .where(Article.story_id.is_not(None))
            .group_by(Article.story_id)
        )
    ).all():
        if story_id in ids:
            first[story_id] = (count, earliest)
    recent: dict[int, int] = {}
    for story_id, count in (
        await session.execute(
            select(Article.story_id, func.count(Article.id))
            .where(
                Article.story_id.is_not(None),
                func.coalesce(Article.published_at, Article.fetched_at) >= since,
            )
            .group_by(Article.story_id)
        )
    ).all():
        if story_id in ids:
            recent[story_id] = count

    def published_key(row: Any) -> datetime:
        earliest = first.get(row[0], (0, None))[1]
        return earliest if earliest is not None else row[1]

    def top(pool: Sequence[Any], by_sources: bool, reverse: bool) -> list[int]:
        if by_sources:
            pool = sorted(pool, key=lambda r: (recent.get(r[0], 0), r[1], r[0]), reverse=reverse)
        else:
            pool = sorted(pool, key=lambda r: (published_key(r), r[1], r[0]), reverse=reverse)
        return [r[0] for r in pool[:limit]]

    unread = [r for r in rows if not r[2]]
    recent_rows = [r for r in rows if recent.get(r[0], 0) > 0]
    recent_unread = [r for r in recent_rows if not r[2]]
    chosen_ids = {
        "latest": top(rows, False, True),
        "latest_unread": top(unread, False, True),
        "oldest": top(rows, False, False),
        "oldest_unread": top(unread, False, False),
        "most_sources": top(recent_rows, True, True),
        "most_sources_unread": top(recent_unread, True, True),
    }
    wanted = {i for pool in chosen_ids.values() for i in pool}
    stories = {
        st.id: st for st in (await session.scalars(select(Story).where(Story.id.in_(wanted)))).all()
    }
    items = {item.id: item for item in await _page_items(session, user, list(stories.values()))}
    return WidgetSnapshotOut(
        unread_count=len(unread),
        lists={name: [items[i] for i in pool] for name, pool in chosen_ids.items()},
    )


class StoryCountsOut(BaseModel):
    """Counts behind the story-list filter pills (all / unread / updated / saved)."""

    all: int
    unread: int
    updated: int
    saved: int


@router.get("/counts")
async def story_counts(
    category: str | None = None,
    feed: int | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryCountsOut:
    """Filter-pill counts for the user's visible stories — no story payloads loaded."""
    user_feed_ids = await _user_feed_ids(session, user)
    if not user_feed_ids or (feed is not None and feed not in user_feed_ids):
        return StoryCountsOut(all=0, unread=0, updated=0, saved=0)
    visible = select(Article.story_id).where(
        Article.feed_id.in_([feed] if feed is not None else user_feed_ids),
        Article.story_id.is_not(None),
    )
    unread = _is_read_sql().is_(False)
    updated = and_(StoryState.is_read.is_(True), Story.version > StoryState.read_at_version)
    query = (
        select(
            func.count(),
            func.coalesce(func.sum(case((unread, 1), else_=0)), 0),
            func.coalesce(func.sum(case((updated, 1), else_=0)), 0),
            func.coalesce(func.sum(case((StoryState.saved_at.is_not(None), 1), else_=0)), 0),
        )
        .select_from(Story)
        .outerjoin(StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id))
        .where(Story.id.in_(visible))
    )
    if category:
        query = query.where(Story.category == category)
    total, n_unread, n_updated, n_saved = (await session.execute(query)).one()
    return StoryCountsOut(all=total, unread=n_unread, updated=n_updated, saved=n_saved)


class PresetCountsOut(BaseModel):
    counts: dict[str, int]


@router.get("/preset-counts")
async def preset_counts(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PresetCountsOut:
    """Story count per enabled preset with ``show_count`` (manual filters ignored).

    Status-only presets share one conditional-aggregate query; every other preset costs
    one count query."""
    user_feed_ids = await _user_feed_ids(session, user)
    presets = [
        p
        for p in presets_svc.visible_presets(
            presets_svc.load_presets(user.story_presets), user_feed_ids
        )
        if p["enabled"] and p["show_count"]
    ]
    if not user_feed_ids:
        return PresetCountsOut(counts={p["id"]: 0 for p in presets})
    visible = select(Article.story_id).where(
        Article.feed_id.in_(user_feed_ids), Article.story_id.is_not(None)
    )
    joined = (
        select(Story.id)
        .select_from(Story)
        .outerjoin(StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id))
    )

    def only_status(p: dict[str, Any]) -> bool:
        return bool(p["criteria"] == presets_svc.empty_criteria(p["criteria"]["status"]))

    by_status: dict[str, int] = {}
    if any(only_status(p) for p in presets):
        unread = _is_read_sql().is_(False)
        updated = and_(StoryState.is_read.is_(True), Story.version > StoryState.read_at_version)
        total, n_unread, n_updated, n_saved = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(case((unread, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((updated, 1), else_=0)), 0),
                    func.coalesce(
                        func.sum(case((StoryState.saved_at.is_not(None), 1), else_=0)), 0
                    ),
                )
                .select_from(Story)
                .outerjoin(
                    StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id)
                )
                .where(Story.id.in_(visible))
            )
        ).one()
        by_status = {"any": total, "unread": n_unread, "updated": n_updated, "saved": n_saved}
    now = utcnow()
    kinds: dict[int, str] | None = None
    counts: dict[str, int] = {}
    for p in presets:
        if only_status(p):
            counts[p["id"]] = by_status[p["criteria"]["status"]]
            continue
        if kinds is None:
            kinds = await _feed_source_kinds(session, user_feed_ids)
        conditions = _criteria_conditions(p["criteria"], kinds, now)
        if conditions is None:
            counts[p["id"]] = 0
            continue
        counts[p["id"]] = int(
            await session.scalar(
                select(func.count()).select_from(
                    joined.where(Story.id.in_(visible), *conditions).subquery()
                )
            )
            or 0
        )
    return PresetCountsOut(counts=counts)


class FacetCount(BaseModel):
    key: str
    count: int


class StoryFacetsOut(BaseModel):
    """Story counts per category and per feed under the current list filter."""

    categories: list[FacetCount]
    feeds: list[FacetCount]


@router.get("/facets")
async def story_facets(
    filter: str = Query(default="all", pattern="^(all|unread|updated)$"),
    saved: bool = False,
    category: str | None = None,
    feed: int | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryFacetsOut:
    """Counts shown beside each category and feed in the filter menu.

    Category counts honour the selected feed and feed counts honour the selected
    category, so every menu entry shows what picking it would return.
    """
    empty = StoryFacetsOut(categories=[], feeds=[])
    user_feed_ids = await _user_feed_ids(session, user)
    if not user_feed_ids:
        return empty
    # filtered stories first (small for "unread"), then their distinct (feed, story) pairs
    filtered = (
        select(Story.id.label("sid"), Story.category.label("category"))
        .outerjoin(StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id))
        .where(*_flag_conditions(filter, saved))
        .subquery()
    )
    pairs = (
        await session.execute(
            select(Article.feed_id, filtered.c.sid, filtered.c.category)
            .join(filtered, filtered.c.sid == Article.story_id)
            .where(Article.feed_id.in_(user_feed_ids))
            .distinct()
        )
    ).all()
    feeds_of: dict[int, set[int]] = {}
    category_of: dict[int, str] = {}
    for feed_id, story_id, story_category in pairs:
        feeds_of.setdefault(story_id, set()).add(feed_id)
        category_of[story_id] = story_category
    category_counts: Counter[str] = Counter()
    feed_counts: Counter[int] = Counter()
    for story_id, story_feeds in feeds_of.items():
        # category counts honour the selected feed, feed counts the selected category
        if feed is None or feed in story_feeds:
            category_counts[category_of[story_id]] += 1
        if not category or category_of[story_id] == category:
            feed_counts.update(story_feeds)
    return StoryFacetsOut(
        categories=[FacetCount(key=k, count=c) for k, c in sorted(category_counts.items())],
        feeds=[FacetCount(key=str(k), count=c) for k, c in sorted(feed_counts.items())],
    )


@router.get("/feed-options")
async def feed_options(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FeedOption]:
    """Feeds subscribed by the user usable in the story-list feed filter."""
    rows = (
        await session.execute(
            select(Feed.id, Feed.title, Feed.kind, Feed.url, Feed.sender_email)
            .join(Article, Article.feed_id == Feed.id)
            .join(UserFeed, UserFeed.feed_id == Feed.id)
            .where(UserFeed.user_id == user.id, Article.story_id.is_not(None))
            .group_by(Feed.id)
            .order_by(Feed.title)
        )
    ).all()
    return [
        FeedOption(id=fid, title=title, kind=kind, url=url, sender_email=sender)
        for fid, title, kind, url, sender in rows
    ]


class ShareLanguage(BaseModel):
    code: str
    name: str


class ShareLanguagesOut(BaseModel):
    summary_language: str
    languages: list[ShareLanguage]


# Declared before /{story_id} so the literal path wins over the int param.
@router.get("/share-languages")
async def share_languages(user: User = Depends(current_user)) -> ShareLanguagesOut:
    """Languages offered by the share picker (from SHARE_LANGUAGES)."""
    from app.services import share

    return ShareLanguagesOut(
        summary_language=settings.summary_language,
        languages=[ShareLanguage.model_validate(lang) for lang in share.available_languages()],
    )


class ListenTextOut(BaseModel):
    article_id: int
    language: str
    text: str
    translated: bool
    content_status: str


@router.get("/articles/{article_id}/listen-text")
async def article_listen_text(
    article_id: int,
    lang: Literal["original", "summary"] = Query("original"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ListenTextOut:
    """Cleaned full text of one article for the Listen player (optionally translated)."""
    article = await session.get(Article, article_id)
    if article is None or article.story_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")
    await _require_visible_story(session, user, article.story_id)
    cleaned = listen_text.usable_text(article.content_status, article.full_text)
    if not cleaned:
        raise HTTPException(status.HTTP_409_CONFLICT, "No usable full text for this article")

    language = article.language
    translated = False
    if lang == "summary":
        target = user.summary_language or settings.summary_language
        if target and article.language and target != article.language:
            try:
                cleaned = await listen_text.translate_article_text(
                    session, article, cleaned, target
                )
            except listen_text.TranslationTooLongError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "Article too long to translate"
                ) from exc
            except llm_client.LLMError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Translation failed") from exc
            translated = True
            language = target
    return ListenTextOut(
        article_id=article.id,
        language=language,
        text=cleaned,
        translated=translated,
        content_status=article.content_status,
    )


@router.get("/{story_id}")
async def story_detail(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryDetail:
    story = await _require_visible_story(session, user, story_id)
    articles = (
        await session.scalars(
            select(Article)
            .where(Article.story_id == story_id)
            .options(selectinload(Article.feed))
            .order_by(Article.id)
        )
    ).all()
    state = await session.get(StoryState, (user.id, story_id))
    is_read, updated = _flags(state, story)
    is_saved, saved_at = _saved_flag(state)
    revisions = (
        await session.scalars(
            select(StoryRevision)
            .where(StoryRevision.story_id == story_id)
            .order_by(StoryRevision.version)
        )
    ).all()

    detail_title = story.title
    detail_summary = story.summary
    user_lang = user.summary_language or settings.summary_language
    story_lang = story.language or settings.summary_language
    if user_lang != story_lang:
        from app.services.translation import get_or_translate_story

        detail_title, detail_summary = await get_or_translate_story(session, story, user_lang)

    detail_video = next((_video_fields(a) for a in articles if a.media_kind == "video"), {})
    return StoryDetail(
        id=story.id,
        title=detail_title,
        summary=detail_summary,
        category=story.category,
        image_url=story.image_url,
        version=story.version,
        is_frozen=story.is_frozen,
        first_seen_at=story.first_seen_at,
        last_updated_at=story.last_updated_at,
        published_at=min(
            (a.published_at for a in articles if a.published_at is not None),
            default=None,
        ),
        is_read=is_read,
        updated_since_read=updated,
        saved=is_saved,
        saved_at=saved_at,
        articles=[_article_out(a) for a in articles],
        revisions=[RevisionOut.model_validate(r) for r in revisions],
        readeck_bookmark_id=story.readeck_bookmark_id,
        **detail_video,
    )


class SavedOut(BaseModel):
    saved: bool
    saved_at: datetime | None


@router.put("/{story_id}/saved")
async def set_saved(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SavedOut:
    await _require_visible_story(session, user, story_id)
    state = await session.get(StoryState, (user.id, story_id))
    if state is None:
        state = StoryState(user_id=user.id, story_id=story_id)
        session.add(state)
    if state.saved_at is None:
        state.saved_at = datetime.now(UTC)
    await session.commit()
    return SavedOut(saved=True, saved_at=state.saved_at)


@router.delete("/{story_id}/saved")
async def clear_saved(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> SavedOut:
    await _require_visible_story(session, user, story_id)
    state = await session.get(StoryState, (user.id, story_id))
    if state is not None and state.saved_at is not None:
        state.saved_at = None
        await session.commit()
    return SavedOut(saved=False, saved_at=None)


class ReadeckOut(BaseModel):
    bookmark_id: str
    href: str
    latency_ms: int


@router.post("/{story_id}/readeck")
async def save_to_readeck(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReadeckOut:
    """Push the story to the configured Readeck instance as a permanent bookmark.

    Optional feature: 404 when readeck_base_url/readeck_token aren't set
    (env or settings overrides). Emits activity events per invariant 6.
    """
    from app.services import readeck

    if not readeck.is_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Readeck integration is not configured")
    story = await _require_visible_story(session, user, story_id)
    articles = list(
        (
            await session.scalars(
                select(Article).where(Article.story_id == story_id).order_by(Article.id)
            )
        ).all()
    )
    await activity.emit(
        session, "readeck", "save_start", {"story_id": story.id, "sources": len(articles)}
    )
    await session.commit()
    try:
        result = await readeck.save_story(story, articles)
    except readeck.ReadeckError as exc:
        await activity.emit(
            session,
            "readeck",
            "save_failed",
            {"story_id": story.id, "error": str(exc)},
            level="error",
        )
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    story.readeck_bookmark_id = result["bookmark_id"] or None
    await activity.emit(
        session,
        "readeck",
        "save_done",
        {
            "story_id": story.id,
            "bookmark_id": result["bookmark_id"],
            "latency_ms": result["latency_ms"],
        },
    )
    await session.commit()
    return ReadeckOut(
        bookmark_id=result["bookmark_id"],
        href=result["href"],
        latency_ms=result["latency_ms"],
    )


class ShareIn(BaseModel):
    # ISO code from GET /stories/share-languages; null/"" = share as-is (no LLM call)
    language: str | None = None


class ShareOut(BaseModel):
    title: str
    text: str
    url: str
    language: str
    translated: bool
    latency_ms: int


@router.post("/{story_id}/share")
async def share_story(
    story_id: int,
    body: ShareIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ShareOut:
    """Build the share card (headline + summary + source links), translated on
    demand via the LLM. Always available — no external service to configure.
    Emits activity events per invariant 6."""
    from app.services import llm_client, share

    story = await _require_visible_story(session, user, story_id)
    articles = list(
        (
            await session.scalars(
                select(Article).where(Article.story_id == story_id).order_by(Article.id)
            )
        ).all()
    )
    await activity.emit(
        session,
        "share",
        "prepare_start",
        {"story_id": story.id, "language": body.language or "original"},
    )
    await session.commit()
    try:
        result = await share.prepare_share(story, articles, body.language)
    except share.ShareError as exc:
        await activity.emit(
            session,
            "share",
            "prepare_failed",
            {"story_id": story.id, "error": str(exc)},
            level="error",
        )
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except llm_client.LLMError as exc:
        await activity.emit(
            session,
            "share",
            "prepare_failed",
            {"story_id": story.id, "error": str(exc)},
            level="error",
        )
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await activity.emit(
        session,
        "share",
        "prepare_done",
        {
            "story_id": story.id,
            "language": result["language"],
            "translated": result["translated"],
            "latency_ms": result["latency_ms"],
        },
    )
    if result["translated"]:
        # Token usage of the on-demand translation (metrics, SPEC §3 llm_usage)
        from app.services import usage

        usage.record(
            session,
            "share_translate",
            endpoint="chat",
            model=settings.llm_model,
            latency_ms=result["latency_ms"],
            story_id=story.id,
            prompt_chars=len(story.title) + len(story.summary),
            completion_chars=len(result["title"]) + len(result["text"]),
        )
    await session.commit()
    return ShareOut.model_validate(result)


class ReprocessOut(BaseModel):
    chars: int
    path: str
    content_status: str
    content_warning: str | None
    requeued: bool


@router.post("/articles/{article_id}/reprocess")
async def reprocess_article(
    article_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReprocessOut:
    """Re-run the full-text fetch chain for one article on demand.

    If the fetched text differs substantially from what we had, the pipeline
    state is reset so the article is re-summarized/re-embedded/re-clustered
    (resumability invariant 7 makes this safe).
    """
    article = await session.get(Article, article_id)
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")
    feed = await session.get(Feed, article.feed_id)
    if feed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")

    old_len = len(article.full_text or "")
    await fetch_full_text(session, article, feed)
    await session.commit()

    new_len = len(article.full_text or "")
    await session.execute(
        delete(ArticleTranslation).where(ArticleTranslation.article_id == article.id)
    )
    await session.commit()
    # Requeue through the LLM pipeline only when content actually changed much
    requeued = abs(new_len - old_len) > max(200, int(0.2 * max(old_len, 1)))
    if requeued:
        await get_vector_store(session).delete_article(article.id)
        article.summary = None
        article.category = None
        article.language = ""
        article.processing_state = "fulltext"
        process.enqueue_article(article.id)
        await activity.emit(
            session,
            "fulltext",
            "manual_reprocess",
            {"article_id": article.id, "old_chars": old_len, "new_chars": new_len},
        )
        await session.commit()

    return ReprocessOut(
        chars=new_len,
        path="full" if article.content_status == "full" else "rss_only",
        content_status=article.content_status,
        content_warning=article.content_warning,
        requeued=requeued,
    )


class ImportUrlIn(BaseModel):
    url: str


class ImportUrlOut(BaseModel):
    article_id: int
    feed_id: int
    already_known: bool
    story_id: int | None


@router.post("/import-url", status_code=status.HTTP_201_CREATED)
async def import_shared_url(
    body: ImportUrlIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ImportUrlOut:
    """Turn a URL shared from another app into an article of the user's "Shared with NewsGator"
    feed; it is then processed and clustered like any other article."""
    article, already_known = await saved_links.import_url(session, user, body.url)
    if not already_known:
        process.enqueue_article(article.id)
    return ImportUrlOut(
        article_id=article.id,
        feed_id=article.feed_id,
        already_known=already_known,
        story_id=article.story_id,
    )


@router.post("/{story_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    story = await _require_visible_story(session, user, story_id)
    state = await session.get(StoryState, (user.id, story_id))
    if state is None:
        state = StoryState(user_id=user.id, story_id=story_id)
        session.add(state)
    state.is_read = True
    state.read_at_version = story.version  # invariant 4: remember the version read
    state.read_at = datetime.now(UTC)
    await session.commit()

    # Outbound sync: push read state to connected third-party reader APIs
    from app.services.readers.sync import sync_story_read_state_outbound

    await sync_story_read_state_outbound(session, user.id, story_id, is_read=True)


@router.post("/{story_id}/unread", status_code=status.HTTP_204_NO_CONTENT)
async def mark_unread(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    state = await session.get(StoryState, (user.id, story_id))
    if state is not None:
        state.is_read = False
        await session.commit()

    # Outbound sync: push unread state to connected third-party reader APIs
    from app.services.readers.sync import sync_story_read_state_outbound

    await sync_story_read_state_outbound(session, user.id, story_id, is_read=False)


@router.get("/{story_id}/diff")
async def story_diff(
    story_id: int,
    from_version: int = Query(alias="from", ge=1),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """What changed since a given version (SPEC §5/§6)."""
    await _require_visible_story(session, user, story_id)
    revisions = (
        await session.scalars(
            select(StoryRevision)
            .where(
                StoryRevision.story_id == story_id,
                StoryRevision.version > from_version,
            )
            .order_by(StoryRevision.version)
        )
    ).all()
    return {
        "from_version": from_version,
        "changes": [
            {"version": r.version, "summary": r.summary, "at": r.created_at} for r in revisions
        ],
    }


class SimilarStoryOut(BaseModel):
    id: int
    title: str
    # exact cosine similarity to the query vector; None = unscored fallback
    # (no centroid/embedding available → recency order)
    similarity: float | None


# Candidate cap for the merge/move pickers — plenty for a dropdown.
SIMILAR_LIMIT = 50


async def _rank_candidates(
    session: AsyncSession,
    query_vec: list[float],
    exclude_ids: set[int],
) -> list[SimilarStoryOut]:
    """ANN candidates, re-ranked by exact cosine.

    sqlite-vec's raw ANN score is a 1/(1+L2) proxy, not cosine — re-ranking
    (same pattern as cluster.py) keeps the percentage shown in the GUI honest
    across all vector backends.
    """
    store = get_vector_store(session)
    qv = np.asarray(query_vec, dtype=np.float32)
    ranked: list[SimilarStoryOut] = []
    for sid, _approx in await store.search_story_centroids(query_vec, limit=SIMILAR_LIMIT):
        if sid in exclude_ids:
            continue
        story = await session.get(Story, sid)
        if story is None:
            continue
        centroid = await store.get_story_centroid(sid)
        if centroid is None:
            continue
        ranked.append(
            SimilarStoryOut(
                id=sid,
                title=story.title,
                similarity=cosine_similarity(qv, np.asarray(centroid)),
            )
        )
    ranked.sort(key=lambda c: c.similarity or 0.0, reverse=True)
    return ranked


async def _recent_candidates(session: AsyncSession, exclude_ids: set[int]) -> list[SimilarStoryOut]:
    """No query vector available — fall back to most-recent stories, unscored."""
    stories = (
        await session.scalars(
            select(Story)
            .order_by(Story.last_updated_at.desc())
            .limit(SIMILAR_LIMIT + len(exclude_ids))
        )
    ).all()
    return [
        SimilarStoryOut(id=s.id, title=s.title, similarity=None)
        for s in stories
        if s.id not in exclude_ids
    ][:SIMILAR_LIMIT]


@router.get("/{story_id}/similar")
async def similar_stories(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SimilarStoryOut]:
    """Merge candidates for the story picker, ranked by proximity
    (centroid-vs-centroid cosine). Read-only probe — no activity event."""
    await _require_visible_story(session, user, story_id)
    centroid = await get_vector_store(session).get_story_centroid(story_id)
    if centroid is None:
        return await _recent_candidates(session, {story_id})
    return await _rank_candidates(session, centroid, {story_id})


@router.get("/articles/{article_id}/similar-stories")
async def similar_stories_for_article(
    article_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SimilarStoryOut]:
    """Move-target candidates for a source, ranked by proximity (article
    embedding vs story centroids). Read-only probe — no activity event."""
    article = await session.get(Article, article_id)
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Article not found")
    exclude = {article.story_id} if article.story_id is not None else set()
    vec = await get_vector_store(session).get_article_vector(article_id)
    if vec is None:
        return await _recent_candidates(session, exclude)
    return await _rank_candidates(session, vec, exclude)


MAX_LISTEN_COUNTER = 1_000_000


class ListenPositionIn(BaseModel):
    step: Literal["summary", "article", "video"]
    article_id: str | None = None
    chunk: int = Field(ge=0, le=MAX_LISTEN_COUNTER)
    elapsed_seconds: int = Field(ge=0, le=MAX_LISTEN_COUNTER)
    story_version: int = Field(ge=0)
    text_length: int | None = Field(default=None, ge=0)


class ListenPositionOut(BaseModel):
    story_id: int
    step: Literal["summary", "article", "video"]
    article_id: str | None
    chunk: int
    elapsed_seconds: int
    story_version: int
    text_length: int | None
    updated_at: datetime


def _listen_position_out(pos: ListenPosition) -> ListenPositionOut:
    return ListenPositionOut(
        story_id=pos.story_id,
        step=pos.step,  # type: ignore[arg-type]
        article_id=str(pos.article_id) if pos.article_id is not None else None,
        chunk=pos.chunk,
        elapsed_seconds=pos.elapsed_seconds,
        story_version=pos.story_version,
        text_length=pos.text_length,
        updated_at=pos.updated_at,
    )


async def _listen_position_valid(
    session: AsyncSession, story: Story, pos: ListenPosition
) -> bool:
    """A position is valid only for the story version and article text it was stored against."""
    if pos.story_version != story.version:
        return False
    if pos.step != "article":
        return True
    article = await session.get(Article, pos.article_id) if pos.article_id is not None else None
    if article is None or article.story_id != story.id:
        return False
    return listen_text.full_text_info(article)[1] == pos.text_length


@router.get(
    "/{story_id}/listen-position",
    response_model=ListenPositionOut,
    responses={204: {"description": "No position stored"}},
)
async def get_listen_position(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ListenPositionOut | Response:
    """Where this user stopped listening to the story (204 when none)."""
    story = await _require_visible_story(session, user, story_id)
    pos = await session.get(ListenPosition, (user.id, story_id))
    if pos is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if not await _listen_position_valid(session, story, pos):
        await session.delete(pos)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return _listen_position_out(pos)


@router.put("/{story_id}/listen-position", response_model=ListenPositionOut)
async def put_listen_position(
    story_id: int,
    body: ListenPositionIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ListenPositionOut:
    """Upsert the user's Listen resume point (last write wins).

    Not a pipeline transition, so no activity event is emitted (invariant 6 covers
    pipeline state only; this is a high-frequency per-user UI bookmark).
    """
    story = await _require_visible_story(session, user, story_id)
    if body.story_version != story.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Story version changed; refetch the story"
        )
    article_pk: int | None = None
    text_length: int | None = None
    if body.step == "article":
        raw_id = body.article_id
        if raw_id is None or not raw_id.isascii() or not raw_id.isdigit():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "article_id is required for step 'article'"
            )
        article_pk = int(raw_id)
        article = await session.get(Article, article_pk)
        if article is None or article.story_id != story_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "article_id is not an article of this story"
            )
        if body.text_length is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "text_length is required for step 'article'"
            )
        text_length = body.text_length
    pos = await session.get(ListenPosition, (user.id, story_id))
    if pos is None:
        pos = ListenPosition(user_id=user.id, story_id=story_id)
        session.add(pos)
    pos.step = body.step
    pos.article_id = article_pk
    pos.chunk = body.chunk
    pos.elapsed_seconds = body.elapsed_seconds
    pos.story_version = story.version
    pos.text_length = text_length
    pos.updated_at = utcnow()
    await session.commit()
    return _listen_position_out(pos)


@router.delete("/{story_id}/listen-position", status_code=status.HTTP_204_NO_CONTENT)
async def delete_listen_position(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _require_visible_story(session, user, story_id)
    await session.execute(
        delete(ListenPosition).where(
            ListenPosition.user_id == user.id, ListenPosition.story_id == story_id
        )
    )
    await session.commit()


class MergeIn(BaseModel):
    source_story_id: int


@router.post("/{story_id}/merge", status_code=status.HTTP_204_NO_CONTENT)
async def merge_story(
    story_id: int,
    body: MergeIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Merge source story into this one; corrections logged as labeled pairs."""
    target = await _require_visible_story(session, user, story_id)
    source = await _require_visible_story(session, user, body.source_story_id)
    if target.id == source.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot merge a story into itself")

    moved = (await session.scalars(select(Article).where(Article.story_id == source.id))).all()
    for article in moved:
        article.story_id = target.id
        session.add(OverridePair(article_id=article.id, story_id=target.id, label="same"))
        await saved_links.flag_shared_article_story(session, article)
    target.last_updated_at = datetime.now(UTC)
    if target.image_url is None:
        target.image_url = source.image_url
    await get_vector_store(session).delete_story(source.id)
    await session.execute(delete(StoryTranslation).where(StoryTranslation.story_id == source.id))
    await session.execute(delete(ListenPosition).where(ListenPosition.story_id == source.id))
    await activity.emit(
        session,
        "cluster",
        "manual_merge",
        {"target": target.id, "source": source.id, "articles": len(moved)},
    )
    from app.services.translation import enqueue_story_translation

    enqueue_story_translation(target.id)
    await session.delete(source)
    await session.commit()


class MoveIn(BaseModel):
    story_id: int


@router.post("/articles/{article_id}/move", status_code=status.HTTP_204_NO_CONTENT)
async def move_article(
    article_id: int,
    body: MoveIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Move an article to a different story (manual clustering correction)."""
    article = await session.get(Article, article_id)
    if article is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if article.story_id is not None:
        await _require_visible_story(session, user, article.story_id)
    target = await session.get(Story, body.story_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    # An empty story has no feed owner yet; any other target must be visible to the caller.
    if await session.scalar(select(Article.id).where(Article.story_id == target.id).limit(1)):
        await _require_visible_story(session, user, target.id)
    if article.story_id is not None and article.story_id != target.id:
        # correction: "this article does NOT belong to its old story"
        session.add(
            OverridePair(article_id=article.id, story_id=article.story_id, label="different")
        )
    session.add(OverridePair(article_id=article.id, story_id=target.id, label="same"))
    article.story_id = target.id
    await saved_links.flag_shared_article_story(session, article)
    target.last_updated_at = datetime.now(UTC)
    await activity.emit(
        session,
        "cluster",
        "manual_move",
        {"article_id": article.id, "story_id": target.id},
    )
    await session.commit()
