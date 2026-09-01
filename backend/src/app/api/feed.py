"""RSS 2.0 feed of stories (SPEC §6): the clustered output as a subscribable feed.

Each <item> is one story: headline, merged summary (already in SUMMARY_LANGUAGE —
invariant 1), lead image as media:content, primary source article (earliest
published) as link. guid is the stable `story:{id}`; pubDate is the original
article publication date, and atom:updated tracks the latest revision date so a
version bump (real content change — invariant 3) shows as an update without
re-notifying the item, while source-only attaches change neither.

Auth reuses `current_user`, so RSS readers pass `?token=` (they cannot set
headers or persist cookies). Read-only — no LLM, no activity events.
"""

import html as html_module
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.db import get_session
from app.models import Article, Story, StoryRevision, StoryState, User

router = APIRouter(tags=["feed"])

MEDIA_NS = "http://search.yahoo.com/mrss/"
ATOM_NS = "http://www.w3.org/2005/Atom"


class FeedItem:
    """One story rendered as an RSS item."""

    def __init__(
        self,
        story: Story,
        link: str | None,
        published_at: datetime | None,
        updated_at: datetime,
    ) -> None:
        self.story = story
        self.link = link
        self.published_at = published_at
        self.updated_at = updated_at


def _rfc822(dt: datetime) -> str:
    """SQLite datetimes are naive — they are UTC by convention."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return format_datetime(dt)


def _summary_to_html(summary: str) -> str:
    paragraphs = [p.strip() for p in summary.split("\n\n") if p.strip()]
    return "".join(f"<p>{html_module.escape(p)}</p>" for p in paragraphs)


def render_rss(items: list[FeedItem], *, channel_title: str, self_url: str) -> str:
    """Serialize items to an RSS 2.0 document (newest first)."""
    rss = Element("rss", version="2.0")
    rss.set("xmlns:media", MEDIA_NS)
    rss.set("xmlns:atom", ATOM_NS)
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = channel_title
    SubElement(channel, "link").text = self_url
    SubElement(channel, "description").text = "NewsGator clustered stories"
    SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        href=self_url,
        rel="self",
        type="application/rss+xml",
    )
    for item in items:
        story = item.story
        el = SubElement(channel, "item")
        SubElement(el, "title").text = story.title
        if item.link:
            SubElement(el, "link").text = item.link
        SubElement(el, "guid", isPermaLink="false").text = f"story:{story.id}"
        SubElement(el, "pubDate").text = _rfc822(
            item.published_at or item.updated_at
        )
        SubElement(el, f"{{{ATOM_NS}}}updated").text = (
            item.updated_at.replace(tzinfo=UTC)
            if item.updated_at.tzinfo is None
            else item.updated_at
        ).isoformat()
        SubElement(el, "category").text = story.category
        SubElement(el, "description").text = _summary_to_html(story.summary)
        if story.image_url:
            SubElement(
                el,
                f"{{{MEDIA_NS}}}content",
                url=story.image_url,
                medium="image",
            )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(
        rss, encoding="unicode"
    )


@router.get("/feed.xml")
async def story_feed(
    request: Request,
    category: str | None = None,
    unread: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    stories = (
        await session.scalars(
            select(Story).order_by(Story.last_updated_at.desc()).limit(limit)
        )
    ).all()

    states = {
        s.story_id
        for s in (
            await session.scalars(
                select(StoryState).where(
                    StoryState.user_id == user.id, StoryState.is_read.is_(True)
                )
            )
        ).all()
    }

    # Primary link per story = earliest published article (else any article).
    article_rows = (
        await session.execute(
            select(Article.story_id, Article.url, Article.published_at).where(
                Article.story_id.is_not(None)
            )
        )
    ).all()
    primary: dict[int, str] = {}
    first_published: dict[int, datetime] = {}
    for story_id, url, published in article_rows:
        if story_id is None:
            continue
        primary.setdefault(story_id, url)
        if published is not None:
            prev = first_published.get(story_id)
            if prev is None or published < prev:
                first_published[story_id] = published
                primary[story_id] = url

    # Date the current version was created — surfaced as atom:updated so a
    # version bump (real content change) marks the item updated without
    # touching pubDate; source-only attaches change neither (invariant 3).
    version_dates: dict[int, datetime] = {
        story_id: latest
        for story_id, latest in (
            await session.execute(
                select(
                    StoryRevision.story_id, func.max(StoryRevision.created_at)
                ).group_by(StoryRevision.story_id)
            )
        ).all()
    }

    items: list[FeedItem] = []
    for story in stories:
        if unread and story.id in states:
            continue
        if category and story.category != category:
            continue
        items.append(
            FeedItem(
                story=story,
                link=primary.get(story.id),
                published_at=first_published.get(story.id),
                updated_at=version_dates.get(story.id, story.last_updated_at),
            )
        )

    title = "NewsGator" + (f" — {category}" if category else "")
    self_url = str(request.url)
    xml = render_rss(items, channel_title=title, self_url=self_url)
    return Response(content=xml, media_type="application/rss+xml")
