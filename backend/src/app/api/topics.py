"""Topic Radar API (SPEC §6): trending topics + per-user mute/follow rules."""

from datetime import datetime, timedelta
from typing import Any, Literal

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
    TopicGroup,
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
    group_id: int | None = None
    rule: TopicRuleBrief | None = None
    # typed by the user: ``id`` is the negated rule id and ``rule`` is always set
    manual: bool = False
    # constellation coordinates in [-1, 1]; only sent with ``?all=true``
    position: list[float] | None = None


class GroupRuleBrief(BaseModel):
    id: int
    mode: str
    until_fade: bool
    expires_at: str | None


class TopicGroupOut(BaseModel):
    id: int
    label: str
    description: str
    topic_count: int
    story_count: int
    unread_count: int
    # (id * 137) % 360: stable colour per group on every client
    hue: int
    position: list[float]
    rule: GroupRuleBrief | None = None


class ManualRuleCreate(BaseModel):
    query: str
    mode: Literal["mute", "follow"] = "follow"
    duration: Literal["7d", "30d", "forever"] = "forever"


class RuleUpdate(BaseModel):
    mode: Literal["mute", "follow"]


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
    group_id: int | None = None
    until_fade: bool
    expires_at: str | None
    created_at: str
    matched_stories: int
    hidden_unread: int
    hidden_count: int = 0


def _rule_out(
    rule: TopicRule, matched: int, hidden_unread: int, hidden_count: int = 0
) -> TopicRuleOut:
    return TopicRuleOut(
        id=rule.id,
        mode=rule.mode,
        label=rule.label,
        topic_id=rule.source_topic_id,
        group_id=rule.source_group_id,
        until_fade=rule.until_fade,
        expires_at=rule.expires_at.isoformat() if rule.expires_at else None,
        created_at=rule.created_at.isoformat(),
        matched_stories=matched,
        hidden_unread=hidden_unread,
        hidden_count=hidden_count,
    )


def _require_enabled() -> None:
    if not settings.topics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topics are disabled")


@router.get("/topics")
async def list_topics(
    all: bool = False,
    group: int | None = None,
    manual_only: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TopicOut]:
    """Topics worth the user's attention: those with a rule of theirs, plus the largest
    shares of their unread stories (at most ``TOPIC_MAX_VISIBLE`` trending ones).

    ``all=true`` returns every active topic with constellation positions instead.
    With ``all=true``: ``group=<id>`` returns only that group's topics, laid out among
    themselves (no manual topics); ``manual_only=true`` returns only the typed topics."""
    _require_enabled()
    feed_ids = await _user_feed_ids(session, user)
    if not feed_ids:
        return []
    visible = select(Article.story_id).where(
        Article.feed_id.in_(feed_ids), Article.story_id.is_not(None)
    )
    topic_query = select(Topic).where(Topic.status == "active")
    if all and group is not None:
        topic_query = topic_query.where(Topic.group_id == group)
    topics = (
        {}
        if all and manual_only
        else {t.id: t for t in (await session.scalars(topic_query)).all()}
    )
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
    all_rules = (
        await session.scalars(select(TopicRule).where(TopicRule.user_id == user.id))
    ).all()
    rules = {r.source_topic_id: r for r in all_rules if r.source_topic_id is not None}
    ruled_story_count: dict[int, int] = {}
    ruled_ids = [r.id for r in rules.values()]
    if ruled_ids:
        ruled_rows = (
            await session.execute(
                select(
                    StoryRuleMatch.rule_id,
                    func.coalesce(StoryState.is_read, False),
                    Story.last_updated_at,
                    _ignored_sql(user.id),
                )
                .join(Story, Story.id == StoryRuleMatch.story_id)
                .outerjoin(
                    StoryState,
                    and_(
                        StoryState.story_id == Story.id,
                        StoryState.user_id == user.id,
                    ),
                )
                .where(
                    StoryRuleMatch.rule_id.in_(ruled_ids),
                    StoryRuleMatch.story_id.in_(visible),
                )
            )
        ).all()
        ruled_by_id = {r.id: r.source_topic_id for r in rules.values()}
        for rule_id, is_read, updated, hidden in ruled_rows:
            tid = ruled_by_id.get(rule_id)
            if tid is None or hidden:
                continue
            if rules[tid].mode == topics_svc.MODE_FOLLOW:
                ruled_story_count[tid] = ruled_story_count.get(tid, 0) + 1
            if not is_read:
                unread[tid] = unread.get(tid, 0) + 1
            ts = updated if updated.tzinfo else updated.replace(tzinfo=recent_cut.tzinfo)
            if ts >= recent_cut:
                recent[tid] = recent.get(tid, 0) + 1
        # Rule matches are the user's live semantic topic. Replace the static
        # StoryTopic contribution rather than counting both representations.
        for topic_id in rules:
            base = [
                row
                for row in rows
                if row[0] == topic_id and not row[4] and not row[2]
            ]
            unread_base = len(base)
            if unread_base:
                unread[topic_id] = max(0, unread.get(topic_id, 0) - unread_base)
            recent_base = sum(
                1
                for row in rows
                if row[0] == topic_id
                and not row[4]
                and (
                    row[3]
                    if row[3].tzinfo
                    else row[3].replace(tzinfo=recent_cut.tzinfo)
                )
                >= recent_cut
            )
            recent[topic_id] = max(0, recent.get(topic_id, 0) - recent_base)
    manual_stars = (
        []
        if all and group is not None and not manual_only
        else await _manual_topics(
            session,
            user,
            [r for r in all_rules if r.source_topic_id is None and r.source_group_id is None],
            recent_cut,
        )
    )

    def out(t: Topic) -> TopicOut:
        rule = rules.get(t.id)
        return TopicOut(
            id=t.id,
            label=t.label,
            description=t.description,
            story_count=ruled_story_count.get(t.id, t.story_count),
            source_count=t.source_count,
            unread_count=unread.get(t.id, 0),
            recent_count=recent.get(t.id, 0),
            last_story_at=t.last_story_at.isoformat(),
            group_id=t.group_id,
            rule=TopicRuleBrief(id=rule.id, mode=rule.mode) if rule else None,
        )

    if all:
        vectors = {t.id: topics_svc.decode(t.centroid) for t in topics.values()}
        for star, rule in manual_stars:
            vectors[star.id] = topics_svc.decode(rule.centroid)
        every = topics_svc.layout_positions(vectors)
        positions = {k: v for k, v in every.items() if k > 0}
        manual_positions = {k: v for k, v in every.items() if k < 0}
        everything = sorted(topics.values(), key=lambda t: t.story_count, reverse=True)
        result = [out(t) for t in everything]
        for item in result:
            item.position = list(positions[item.id])
        for star, _rule in manual_stars:
            star.position = list(manual_positions.get(star.id, (0.0, 0.0, 0.0)))
        return [*result, *(star for star, _ in manual_stars)]

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
    return [out(t) for t in [*trending, *ruled]] + [star for star, _ in manual_stars]


async def _manual_topics(
    session: AsyncSession, user: User, rules: list[TopicRule], recent_cut: datetime
) -> list[tuple[TopicOut, TopicRule]]:
    """Typed topics as stars: their numbers come from the rule's materialized matches."""
    if not rules:
        return []
    rows = (
        await session.execute(
            select(
                StoryRuleMatch.rule_id,
                func.coalesce(StoryState.is_read, False),
                Story.last_updated_at,
                _ignored_sql(user.id),
            )
            .join(Story, Story.id == StoryRuleMatch.story_id)
            .outerjoin(
                StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id)
            )
            .where(StoryRuleMatch.rule_id.in_([r.id for r in rules]))
        )
    ).all()
    stats: dict[int, dict[str, Any]] = {}
    for rid, is_read, updated, hidden in rows:
        st = stats.setdefault(rid, {"n": 0, "unread": 0, "recent": 0, "last": None})
        ts = updated if updated.tzinfo else updated.replace(tzinfo=recent_cut.tzinfo)
        st["n"] += 1
        if not hidden and not is_read:
            st["unread"] += 1
        if not hidden and ts >= recent_cut:
            st["recent"] += 1
        if st["last"] is None or ts > st["last"]:
            st["last"] = ts
    result: list[tuple[TopicOut, TopicRule]] = []
    for rule in rules:
        st = stats.get(rule.id, {"n": 0, "unread": 0, "recent": 0, "last": rule.created_at})
        last = st["last"] or rule.created_at
        result.append(
            (
                TopicOut(
                    id=-rule.id,
                    label=rule.label,
                    description="",
                    story_count=st["n"],
                    source_count=0,
                    unread_count=st["unread"],
                    recent_count=st["recent"],
                    last_story_at=last.isoformat(),
                    rule=TopicRuleBrief(id=rule.id, mode=rule.mode),
                    manual=True,
                ),
                rule,
            )
        )
    return result


@router.get("/topic-groups")
async def list_topic_groups(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TopicGroupOut]:
    """Every topic group (galaxy) with the user's unread count, constellation position and
    the user's rule on it. Typed (manual) topics belong to no group."""
    _require_enabled()
    groups = (await session.scalars(select(TopicGroup).order_by(TopicGroup.id))).all()
    if not groups:
        return []
    unread: dict[int, int] = {}
    feed_ids = await _user_feed_ids(session, user)
    if feed_ids:
        visible = select(Article.story_id).where(
            Article.feed_id.in_(feed_ids), Article.story_id.is_not(None)
        )
        rows = (
            await session.execute(
                select(Topic.group_id, StoryTopic.story_id, _ignored_sql(user.id))
                .join(Topic, Topic.id == StoryTopic.topic_id)
                .join(Story, Story.id == StoryTopic.story_id)
                .outerjoin(
                    StoryState,
                    and_(StoryState.story_id == Story.id, StoryState.user_id == user.id),
                )
                .where(
                    StoryTopic.story_id.in_(visible),
                    Topic.status == "active",
                    Topic.group_id.is_not(None),
                    func.coalesce(StoryState.is_read, False).is_(False),
                )
            )
        ).all()
        for gid, _sid, hidden in rows:
            if not hidden:
                unread[gid] = unread.get(gid, 0) + 1
    rules = {
        r.source_group_id: r
        for r in (
            await session.scalars(
                select(TopicRule).where(
                    TopicRule.user_id == user.id, TopicRule.source_group_id.is_not(None)
                )
            )
        ).all()
    }
    positions = topics_svc.layout_positions(
        {g.id: topics_svc.decode(g.centroid) for g in groups}
    )
    out: list[TopicGroupOut] = []
    for g in groups:
        rule = rules.get(g.id)
        out.append(
            TopicGroupOut(
                id=g.id,
                label=g.label,
                description=g.description,
                topic_count=g.topic_count,
                story_count=g.story_count,
                unread_count=unread.get(g.id, 0),
                hue=(g.id * 137) % 360,
                position=list(positions[g.id]),
                rule=GroupRuleBrief(
                    id=rule.id,
                    mode=rule.mode,
                    until_fade=rule.until_fade,
                    expires_at=rule.expires_at.isoformat() if rule.expires_at else None,
                )
                if rule
                else None,
            )
        )
    return out


@router.get("/topic-groups/{group_id}/preview")
async def preview_topic_group(
    group_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TopicPreviewOut:
    _require_enabled()
    group = await session.get(TopicGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topic group not found")
    feed_ids = await _user_feed_ids(session, user)
    count, samples = await topics_svc.preview_group_mute(session, group, feed_ids, user.id)
    return TopicPreviewOut(
        would_hide_unread=count, samples=[SampleStory(id=i, title=t) for i, t in samples]
    )


@router.post("/topic-groups/{group_id}/rules", status_code=status.HTTP_201_CREATED)
async def create_topic_group_rule(
    group_id: int,
    body: RuleCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TopicRuleOut:
    _require_enabled()
    group = await session.get(TopicGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Topic group not found")
    days = _DURATION_DAYS.get(body.duration)
    rule = await topics_svc.create_group_rule(
        session,
        user.id,
        group,
        body.mode,
        until_fade=body.duration == "fade",
        expires_at=utcnow() + timedelta(days=days) if days else None,
    )
    await session.commit()
    return await _single_rule_out(session, rule, user)


@router.post("/topic-rules/custom", status_code=status.HTTP_201_CREATED)
async def create_manual_topic_rule(
    body: ManualRuleCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TopicRuleOut:
    """Follow or mute a topic the user typed (e.g. "GTA 6"); matched like detected topics."""
    _require_enabled()
    days = _DURATION_DAYS.get(body.duration)
    try:
        rule = await topics_svc.create_manual_rule(
            session,
            user.id,
            body.query,
            body.mode,
            expires_at=utcnow() + timedelta(days=days) if days else None,
        )
    except topics_svc.ManualTopicError as exc:
        raise HTTPException(422, exc.code) from exc
    except Exception as exc:  # embedding server down
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "embedding_failed") from exc
    await session.commit()
    return await _single_rule_out(session, rule, user)


@router.patch("/topic-rules/{rule_id}")
async def update_topic_rule(
    rule_id: int,
    body: RuleUpdate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TopicRuleOut:
    """Switch a rule between mute and follow."""
    _require_enabled()
    rule = await _own_rule(session, user, rule_id)
    await topics_svc.set_rule_mode(session, rule, body.mode)
    await session.commit()
    return await _single_rule_out(session, rule, user)


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
) -> dict[int, tuple[int, int, int]]:
    """rule id -> (matched stories, unread among them, currently hidden among them)."""
    if not rule_ids:
        return {}
    rows = (
        await session.execute(
            select(
                StoryRuleMatch.rule_id,
                func.coalesce(StoryState.is_read, False),
                _ignored_sql(user.id),
            )
            .join(Story, Story.id == StoryRuleMatch.story_id)
            .outerjoin(
                StoryState, and_(StoryState.story_id == Story.id, StoryState.user_id == user.id)
            )
            .where(StoryRuleMatch.rule_id.in_(rule_ids))
        )
    ).all()
    stats: dict[int, tuple[int, int, int]] = {}
    for rid, is_read, hidden in rows:
        matched, unread, hid = stats.get(rid, (0, 0, 0))
        stats[rid] = (matched + 1, unread + (0 if is_read else 1), hid + (1 if hidden else 0))
    return stats


async def _single_rule_out(session: AsyncSession, rule: TopicRule, user: User) -> TopicRuleOut:
    matched, unread, hidden = (await _rule_stats(session, user, [rule.id])).get(
        rule.id, (0, 0, 0)
    )
    mute = rule.mode == "mute"
    return _rule_out(rule, matched, unread if mute else 0, hidden if mute else 0)


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
        matched, unread, hidden = stats.get(r.id, (0, 0, 0))
        mute = r.mode == "mute"
        out.append(_rule_out(r, matched, unread if mute else 0, hidden if mute else 0))
    return out


async def _own_rule(session: AsyncSession, user: User, rule_id: int) -> TopicRule:
    rule = await session.get(TopicRule, rule_id)
    if rule is None or rule.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    return rule


@router.delete("/topic-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic_rule(
    rule_id: int,
    keep_hidden: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _remove_rule(session, user, rule_id, keep_hidden)


async def _remove_rule(
    session: AsyncSession, user: User, rule_id: int, keep_hidden: bool
) -> None:
    rule = await _own_rule(session, user, rule_id)
    if keep_hidden:
        await topics_svc.set_aside_rule_matches(session, rule)
    await topics_svc.delete_rule(session, rule)
    await activity.emit(session, "topics", "rule_deleted", {"rule_id": rule_id})
    await session.commit()


class RemoveRuleIn(BaseModel):
    keep_hidden: bool = False


@router.post("/topic-rules/{rule_id}/remove", status_code=status.HTTP_204_NO_CONTENT)
async def remove_topic_rule(
    rule_id: int,
    body: RemoveRuleIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """``DELETE /topic-rules/{id}?keep_hidden=`` for clients that cannot send query strings."""
    await _remove_rule(session, user, rule_id, body.keep_hidden)


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
