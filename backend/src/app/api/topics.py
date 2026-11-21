"""Topic Radar API (SPEC §6): trending topics + per-user mute/follow rules."""

from datetime import timedelta
from typing import Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.stories import _ignored_sql, _user_feed_ids
from app.core.config import settings
from app.core.db import get_session
from app.models import (
    Article,
    Story,
    StoryRuleMatch,
    StoryState,
    StoryTopic,
    Topic,
    TopicFeedback,
    TopicRule,
    User,
    utcnow,
)
from app.services import activity
from app.services import topics as topics_svc
from app.services.vectorstore import cosine_similarity, get_vector_store

router = APIRouter(tags=["topics"])

Duration = Literal["fade", "7d", "30d", "forever"]
_DURATION_DAYS = {"7d": 7, "30d": 30}
RECENT_HOURS = 48


class TopicRuleBrief(BaseModel):
    id: int
    mode: str


class TopicOut(BaseModel):
    id: int
    label: str
    description: str
    story_count: int
    source_count: int
    # the user's unread, non-ignored stories in this topic
    unread_count: int
    # stories updated in the last 48 h — how fast the topic is growing
    recent_count: int
    last_story_at: str
    rule: TopicRuleBrief | None = None
    # constellation coordinates in [-1, 1]; only sent with ``?all=true``
    position: list[float] | None = None


class SampleStory(BaseModel):
    id: int
    title: str


class TopicPreviewOut(BaseModel):
    would_hide_unread: int
    samples: list[SampleStory]


class RuleCreate(BaseModel):
    mode: Literal["mute", "follow"]
    duration: Duration = "fade"


class TopicRuleOut(BaseModel):
    id: int
    mode: str
    label: str
    topic_id: int | None
    until_fade: bool
    expires_at: str | None
    created_at: str
    matched_stories: int
    hidden_unread: int


def _rule_out(rule: TopicRule, matched: int, hidden_unread: int) -> TopicRuleOut:
    return TopicRuleOut(
        id=rule.id,
        mode=rule.mode,
        label=rule.label,
        topic_id=rule.source_topic_id,
        until_fade=rule.until_fade,
        expires_at=rule.expires_at.isoformat() if rule.expires_at else None,
        created_at=rule.created_at.isoformat(),
        matched_stories=matched,
        hidden_unread=hidden_unread,
    )


def _require_enabled() -> None:
    if not settings.topics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topics are disabled")


@router.get("/topics")
async def list_topics(
    all: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TopicOut]:
    """Topics worth the user's attention: those with a rule of theirs, plus the largest
    shares of their unread stories (at most ``TOPIC_MAX_VISIBLE`` trending ones).

    ``all=true`` returns every active topic with constellation positions instead."""
    _require_enabled()
    feed_ids = await _user_feed_ids(session, user)
    if not feed_ids:
        return []
    visible = select(Article.story_id).where(
        Article.feed_id.in_(feed_ids), Article.story_id.is_not(None)
    )
    topics = {
        t.id: t
        for t in (await session.scalars(select(Topic).where(Topic.status == "active"))).all()
    }
    recent_cut = utcnow() - timedelta(hours=RECENT_HOURS)
    rows = (
        await session.execute(
            select(
                StoryTopic.topic_id,
                StoryTopic.story_id,
                func.coalesce(StoryState.is_read, False),
                Story.last_updated_at,
                _ignored_sql(user.id),
            )
            .join(Story, Story.id == StoryTopic.story_id)
            .outerjoin(
                StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id)
            )
            .where(StoryTopic.story_id.in_(visible), StoryTopic.topic_id.in_(list(topics)))
        )
    ).all()
    unread: dict[int, int] = {}
    recent: dict[int, int] = {}
    for tid, _sid, is_read, updated, hidden in rows:
        if hidden:
            continue
        if not is_read:
            unread[tid] = unread.get(tid, 0) + 1
        ts = updated if updated.tzinfo else updated.replace(tzinfo=recent_cut.tzinfo)
        if ts >= recent_cut:
            recent[tid] = recent.get(tid, 0) + 1
    total_unread = sum(unread.values()) or 1
    rules = {
        r.source_topic_id: r
        for r in (
            await session.scalars(select(TopicRule).where(TopicRule.user_id == user.id))
        ).all()
    }

    def out(t: Topic) -> TopicOut:
        rule = rules.get(t.id)
        return TopicOut(
            id=t.id,
            label=t.label,
            description=t.description,
            story_count=t.story_count,
            source_count=t.source_count,
            unread_count=unread.get(t.id, 0),
            recent_count=recent.get(t.id, 0),
            last_story_at=t.last_story_at.isoformat(),
            rule=TopicRuleBrief(id=rule.id, mode=rule.mode) if rule else None,
        )

    if all:
        positions = topics_svc.layout_positions(
            {t.id: topics_svc.decode(t.centroid) for t in topics.values()}
        )
        everything = sorted(topics.values(), key=lambda t: t.story_count, reverse=True)
        result = [out(t) for t in everything]
        for item in result:
            item.position = list(positions[item.id])
        return result

    trending = sorted(
        (
            t
            for t in topics.values()
            if t.id not in rules
            and unread.get(t.id, 0) / total_unread >= settings.topic_min_user_share
        ),
        key=lambda t: (unread.get(t.id, 0), recent.get(t.id, 0)),
        reverse=True,
    )[: settings.topic_max_visible]
    ruled = [t for t in topics.values() if t.id in rules]
    return [out(t) for t in [*trending, *ruled]]


@router.get("/topics/{topic_id}/preview")
async def preview_topic(
    topic_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TopicPreviewOut:
    _require_enabled()
    topic = await session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topic not found")
    feed_ids = await _user_feed_ids(session, user)
    count, samples = await topics_svc.preview_mute(session, topic, feed_ids, user.id)
    return TopicPreviewOut(
        would_hide_unread=count, samples=[SampleStory(id=i, title=t) for i, t in samples]
    )


@router.post("/topics/{topic_id}/rules", status_code=status.HTTP_201_CREATED)
async def create_topic_rule(
    topic_id: int,
    body: RuleCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TopicRuleOut:
    _require_enabled()
    topic = await session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topic not found")
    days = _DURATION_DAYS.get(body.duration)
    rule = await topics_svc.create_rule(
        session,
        user.id,
        topic,
        body.mode,
        until_fade=body.duration == "fade",
        expires_at=utcnow() + timedelta(days=days) if days else None,
    )
    await session.commit()
    return await _single_rule_out(session, rule, user)


async def _rule_stats(
    session: AsyncSession, user: User, rule_ids: list[int]
) -> dict[int, tuple[int, int]]:
    """rule id -> (matched stories, unread among them)."""
    if not rule_ids:
        return {}
    rows = (
        await session.execute(
            select(StoryRuleMatch.rule_id, func.coalesce(StoryState.is_read, False))
            .outerjoin(
                StoryState,
                and_(
                    StoryState.story_id == StoryRuleMatch.story_id, StoryState.user_id == user.id
                ),
            )
            .where(StoryRuleMatch.rule_id.in_(rule_ids))
        )
    ).all()
    stats: dict[int, tuple[int, int]] = {}
    for rid, is_read in rows:
        matched, unread = stats.get(rid, (0, 0))
        stats[rid] = (matched + 1, unread + (0 if is_read else 1))
    return stats


async def _single_rule_out(session: AsyncSession, rule: TopicRule, user: User) -> TopicRuleOut:
    matched, unread = (await _rule_stats(session, user, [rule.id])).get(rule.id, (0, 0))
    return _rule_out(rule, matched, unread if rule.mode == "mute" else 0)


@router.get("/topic-rules")
async def list_topic_rules(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TopicRuleOut]:
    _require_enabled()
    rules = (
        await session.scalars(
            select(TopicRule).where(TopicRule.user_id == user.id).order_by(TopicRule.id)
        )
    ).all()
    stats = await _rule_stats(session, user, [r.id for r in rules])
    out: list[TopicRuleOut] = []
    for r in rules:
        matched, unread = stats.get(r.id, (0, 0))
        out.append(_rule_out(r, matched, unread if r.mode == "mute" else 0))
    return out


async def _own_rule(session: AsyncSession, user: User, rule_id: int) -> TopicRule:
    rule = await session.get(TopicRule, rule_id)
    if rule is None or rule.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    return rule


@router.delete("/topic-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic_rule(
    rule_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    rule = await _own_rule(session, user, rule_id)
    await topics_svc.delete_rule(session, rule)
    await activity.emit(session, "topics", "rule_deleted", {"rule_id": rule_id})
    await session.commit()


class FeedbackIn(BaseModel):
    story_id: int


@router.post("/topic-rules/{rule_id}/feedback")
async def topic_rule_feedback(
    rule_id: int,
    body: FeedbackIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TopicRuleOut:
    """"This story is not about the topic": stored as a labeled pair (invariant 9) and the
    rule's threshold is raised just above this story's similarity, so it (and anything
    less similar) stops matching."""
    rule = await _own_rule(session, user, rule_id)
    centroid = await get_vector_store(session).get_story_centroid(body.story_id)
    if centroid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
    sim = cosine_similarity(
        np.asarray(centroid, dtype=np.float64), topics_svc.decode(rule.centroid)
    )
    session.add(
        TopicFeedback(user_id=user.id, story_id=body.story_id, rule_id=rule.id, similarity=sim)
    )
    rule.threshold = min(0.99, max(rule.threshold, sim + 0.01))
    await topics_svc.sync_rule_matches(session, rule)
    await session.execute(
        delete(StoryRuleMatch).where(
            StoryRuleMatch.rule_id == rule.id, StoryRuleMatch.story_id == body.story_id
        )
    )
    await activity.emit(
        session,
        "topics",
        "rule_feedback",
        {"rule_id": rule.id, "story_id": body.story_id, "similarity": round(sim, 4)},
    )
    await session.commit()
    return await _single_rule_out(session, rule, user)
