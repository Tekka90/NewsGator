"""Stories API (SPEC §6): list with per-user flags, detail, read/unread, diff,
manual merge/move (logged as labeled pairs — invariant 9)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.db import get_session
from app.models import Article, OverridePair, Story, StoryRevision, StoryState, User
from app.services import activity
from app.services.vectorstore import get_vector_store

router = APIRouter(prefix="/stories", tags=["stories"])


class StoryListItem(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    version: int
    is_frozen: bool
    source_count: int
    last_updated_at: datetime
    is_read: bool
    updated_since_read: bool


class ArticleOut(BaseModel):
    id: int
    title: str
    url: str
    language: str
    summary: str | None
    content_status: str
    content_warning: str | None
    published_at: datetime | None
    feed_id: int

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
    version: int
    is_frozen: bool
    first_seen_at: datetime
    last_updated_at: datetime
    is_read: bool
    updated_since_read: bool
    articles: list[ArticleOut]
    revisions: list[RevisionOut]


def _flags(state: StoryState | None, story: Story) -> tuple[bool, bool]:
    is_read = bool(state and state.is_read)
    updated = is_read and state is not None and story.version > state.read_at_version
    return is_read, updated


@router.get("")
async def list_stories(
    filter: str = Query(default="all", pattern="^(all|unread|updated)$"),
    category: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[StoryListItem]:
    stories = (
        await session.scalars(select(Story).order_by(Story.last_updated_at.desc()))
    ).all()
    states = {
        s.story_id: s
        for s in (
            await session.scalars(select(StoryState).where(StoryState.user_id == user.id))
        ).all()
    }
    counts: dict[int, int] = {}
    for story in stories:
        counts[story.id] = len(
            (
                await session.scalars(
                    select(Article.id).where(Article.story_id == story.id)
                )
            ).all()
        )

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
        out.append(
            StoryListItem(
                id=story.id,
                title=story.title,
                summary=story.summary,
                category=story.category,
                version=story.version,
                is_frozen=story.is_frozen,
                source_count=counts.get(story.id, 0),
                last_updated_at=story.last_updated_at,
                is_read=is_read,
                updated_since_read=updated,
            )
        )
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
            select(Article).where(Article.story_id == story_id).order_by(Article.id)
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
        version=story.version,
        is_frozen=story.is_frozen,
        first_seen_at=story.first_seen_at,
        last_updated_at=story.last_updated_at,
        is_read=is_read,
        updated_since_read=updated,
        articles=[ArticleOut.model_validate(a) for a in articles],
        revisions=[RevisionOut.model_validate(r) for r in revisions],
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
