"""Stories API (SPEC §6): list with per-user flags, detail, read/unread, diff,
manual merge/move (logged as labeled pairs — invariant 9)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import current_user
from app.core.db import get_session
from app.models import (
    Article,
    Feed,
    OverridePair,
    Story,
    StoryRevision,
    StoryState,
    User,
)
from app.services import activity, process
from app.services.fulltext import fetch_full_text
from app.services.vectorstore import get_vector_store

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
    # earliest article publication date in the story (None if unknown)
    published_at: datetime | None
    last_updated_at: datetime
    is_read: bool
    updated_since_read: bool


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
    feed_id: int
    feed_title: str
    feed_url: str

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
    articles: list[ArticleOut]
    revisions: list[RevisionOut]


def _article_out(a: Article) -> ArticleOut:
    return ArticleOut(
        id=a.id,
        title=a.title,
        url=a.url,
        image_url=a.image_url,
        language=a.language,
        summary=a.summary,
        content_status=a.content_status,
        content_warning=a.content_warning,
        published_at=a.published_at,
        feed_id=a.feed_id,
        feed_title=a.feed.title,
        feed_url=a.feed.url,
    )


def _flags(state: StoryState | None, story: Story) -> tuple[bool, bool]:
    is_read = bool(state and state.is_read)
    updated = is_read and state is not None and story.version > state.read_at_version
    return is_read, updated


@router.get("")
async def list_stories(
    filter: str = Query(default="all", pattern="^(all|unread|updated)$"),
    category: str | None = None,
    sort: str = Query(default="published", pattern="^(updated|published|sources)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[StoryListItem]:
    reverse = order == "desc"
    stories = (
        await session.scalars(
            select(Story).order_by(
                Story.last_updated_at.desc() if reverse else Story.last_updated_at
            )
        )
    ).all()
    states = {
        s.story_id: s
        for s in (
            await session.scalars(select(StoryState).where(StoryState.user_id == user.id))
        ).all()
    }
    stats: dict[int, tuple[int, datetime | None]] = {
        story_id: (count, first_published)
        for story_id, count, first_published in (
            await session.execute(
                select(
                    Article.story_id,
                    func.count(Article.id),
                    func.min(Article.published_at),
                ).group_by(Article.story_id)
            )
        ).all()
    }

    out: list[StoryListItem] = []
    for story in stories:
        state = states.get(story.id)
        is_read, updated = _flags(state, story)
        if filter == "unread" and is_read:
            continue
        if filter == "updated" and not updated:
            continue
        if category and story.category != category:
            continue
        source_count, published_at = stats.get(story.id, (0, None))
        out.append(
            StoryListItem(
                id=story.id,
                title=story.title,
                summary=story.summary,
                category=story.category,
                image_url=story.image_url,
                version=story.version,
                is_frozen=story.is_frozen,
                source_count=source_count,
                published_at=published_at,
                last_updated_at=story.last_updated_at,
                is_read=is_read,
                updated_since_read=updated,
            )
        )
    if sort == "published":
        # article publication date; unknown dates always last regardless of order
        out.sort(key=lambda s: (s.published_at is not None, s.published_at), reverse=True)
        if not reverse:
            known = [s for s in out if s.published_at is not None]
            out = known[::-1] + [s for s in out if s.published_at is None]
    elif sort == "sources":
        out.sort(key=lambda s: (s.source_count, s.last_updated_at), reverse=reverse)
    return out


@router.get("/{story_id}")
async def story_detail(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StoryDetail:
    story = await session.get(Story, story_id)
    if story is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
    state = await session.get(StoryState, (user.id, story_id))
    is_read, updated = _flags(state, story)
    articles = (
        await session.scalars(
            select(Article)
            .where(Article.story_id == story_id)
            .options(selectinload(Article.feed))
            .order_by(Article.id)
        )
    ).all()
    revisions = (
        await session.scalars(
            select(StoryRevision)
            .where(StoryRevision.story_id == story_id)
            .order_by(StoryRevision.version)
        )
    ).all()
    return StoryDetail(
        id=story.id,
        title=story.title,
        summary=story.summary,
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
        articles=[_article_out(a) for a in articles],
        revisions=[RevisionOut.model_validate(r) for r in revisions],
    )


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
            session, "fulltext", "manual_reprocess",
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


@router.post("/{story_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    story_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    story = await session.get(Story, story_id)
    if story is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
    state = await session.get(StoryState, (user.id, story_id))
    if state is None:
        state = StoryState(user_id=user.id, story_id=story_id)
        session.add(state)
    state.is_read = True
    state.read_at_version = story.version  # invariant 4: remember the version read
    state.read_at = datetime.now(UTC)
    await session.commit()


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


@router.get("/{story_id}/diff")
async def story_diff(
    story_id: int,
    from_version: int = Query(alias="from", ge=1),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """What changed since a given version (SPEC §5/§6)."""
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
    if not revisions and await session.get(Story, story_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
    return {
        "from_version": from_version,
        "changes": [
            {"version": r.version, "summary": r.summary, "at": r.created_at}
            for r in revisions
        ],
    }


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
    target = await session.get(Story, story_id)
    source = await session.get(Story, body.source_story_id)
    if target is None or source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
    if target.id == source.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot merge a story into itself")

    moved = (
        await session.scalars(select(Article).where(Article.story_id == source.id))
    ).all()
    for article in moved:
        article.story_id = target.id
        session.add(
            OverridePair(article_id=article.id, story_id=target.id, label="same")
        )
    target.last_updated_at = datetime.now(UTC)
    if target.image_url is None:
        target.image_url = source.image_url
    await get_vector_store(session).delete_story(source.id)
    await activity.emit(
        session, "cluster", "manual_merge",
        {"target": target.id, "source": source.id, "articles": len(moved)},
    )
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
    target = await session.get(Story, body.story_id)
    if article is None or target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if article.story_id is not None and article.story_id != target.id:
        # correction: "this article does NOT belong to its old story"
        session.add(
            OverridePair(article_id=article.id, story_id=article.story_id, label="different")
        )
    session.add(OverridePair(article_id=article.id, story_id=target.id, label="same"))
    article.story_id = target.id
    target.last_updated_at = datetime.now(UTC)
    await activity.emit(
        session, "cluster", "manual_move",
        {"article_id": article.id, "story_id": target.id},
    )
    await session.commit()
