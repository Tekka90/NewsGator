"""Topic Radar: discover broad topics, let users mute / follow them.

A topic is a cluster of Story centroids (same EMBED_MODEL vectors as clustering —
invariant 2) over a rolling window; nothing here is language- or domain-specific.
Users never act on a topic row directly: muting/following snapshots the topic centroid
into a ``TopicRule`` that matches stories by cosine similarity, so the rule survives
the topic fading and re-matches when the subject comes back.

* "ignored" is derived: a story matching a mute rule and no follow rule, and not saved.
* Matches are materialized in ``story_rule_match`` so story lists stay a cheap SQL join.
* Pure helpers (``rule_matches``, ``visibility``, ``cluster_vectors``) are mirrored by
  the Apple standalone pipeline and covered by ``tests/fixtures/topic_vectors.json``.
"""

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import numpy as np
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    Article,
    Story,
    StoryRuleMatch,
    StorySetAside,
    StoryState,
    StoryTopic,
    Topic,
    TopicGroup,
    TopicRule,
    utcnow,
)
from app.services import activity, llm_client, llmtrace, prompts, usage
from app.services.vectorstore import cosine_similarity, get_vector_store

VISIBLE = "visible"
IGNORED = "ignored"
FOLLOWED = "followed"
MODE_MUTE = "mute"
MODE_FOLLOW = "follow"


# --- pure helpers (shared contract: tests/fixtures/topic_vectors.json) ---


def encode(vec: Iterable[float]) -> bytes:
    return np.asarray(list(vec), dtype="<f4").tobytes()


def decode(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4").astype(np.float64)


def rule_matches(story_vec: np.ndarray, rule_vec: np.ndarray, threshold: float) -> bool:
    if story_vec.shape != rule_vec.shape:
        return False
    return cosine_similarity(story_vec, rule_vec) >= threshold


def visibility(modes: Iterable[str], *, saved: bool, set_aside: bool = False) -> str:
    """Follow beats mute; saved stories are never ignored. A set-aside story (its mute rule
    ended) counts as muted."""
    found = set(modes)
    if MODE_FOLLOW in found:
        return FOLLOWED
    if (MODE_MUTE in found or set_aside) and not saved:
        return IGNORED
    return VISIBLE


def layout_positions(vectors: dict[int, np.ndarray]) -> dict[int, tuple[float, float, float]]:
    """3-D coordinates in [-1, 1] for the topic constellation.

    Principal-component projection of the topic centroids, so related topics sit close
    together. Deterministic for a given set of centroids; fewer than 3 dimensions of spread
    (e.g. one topic) fall back to the origin on the missing axes.
    """
    ids = sorted(vectors)
    if not ids:
        return {}
    matrix = np.stack([vectors[i] for i in ids]).astype(np.float64)
    centered = matrix - matrix.mean(axis=0)
    # eigen-decomposition of the small n x n Gram matrix instead of the d x d covariance
    values, vecs = np.linalg.eigh(centered @ centered.T)
    order = np.argsort(values)[::-1][:3]
    coords = np.zeros((len(ids), 3))
    for axis, k in enumerate(order):
        scale = float(np.sqrt(max(values[k], 0.0)))
        column = vecs[:, k] * scale
        # fix the eigenvector sign so the layout does not flip between refreshes
        if column[np.argmax(np.abs(column))] < 0:
            column = -column
        coords[:, axis] = column
    peak = float(np.max(np.abs(coords))) or 1.0
    coords /= peak
    return {i: (float(c[0]), float(c[1]), float(c[2])) for i, c in zip(ids, coords, strict=True)}


def cluster_vectors(
    vectors: dict[int, np.ndarray], threshold: float
) -> list[tuple[np.ndarray, list[int]]]:
    """Deterministic greedy centroid clustering (ids ascending, one refinement pass)."""
    clusters: list[tuple[np.ndarray, list[int]]] = []
    for sid in sorted(vectors):
        vec = vectors[sid]
        best, best_sim = -1, threshold
        for i, (centroid, _) in enumerate(clusters):
            if centroid.shape != vec.shape:
                continue
            sim = cosine_similarity(vec, centroid)
            if sim >= best_sim:
                best, best_sim = i, sim
        if best < 0:
            clusters.append((vec.copy(), [sid]))
        else:
            centroid, members = clusters[best]
            members.append(sid)
            clusters[best] = (centroid + (vec - centroid) / len(members), members)
    # refinement: recompute centroids as true means, reassign each story to its best cluster
    centroids = [np.mean([vectors[m] for m in members], axis=0) for _, members in clusters]
    regrouped: dict[int, list[int]] = {}
    for sid in sorted(vectors):
        sims = [
            cosine_similarity(vectors[sid], c) if c.shape == vectors[sid].shape else -1.0
            for c in centroids
        ]
        regrouped.setdefault(int(np.argmax(sims)), []).append(sid)
    return [
        (np.mean([vectors[m] for m in members], axis=0), members)
        for _, members in sorted(regrouped.items())
    ]


def group_topics(vectors: dict[int, np.ndarray]) -> list[list[int]]:
    """Average-linkage agglomerative clustering of topic centroids on cosine similarity.

    Merges the closest pair while its similarity is ``>= TOPIC_GROUP_THRESHOLD``, then keeps
    merging while more than ``TOPIC_GROUP_MAX`` groups remain. Deterministic: ties break on the
    smallest member ids; groups are returned ordered by their smallest id, members ascending.
    """
    ids = sorted(vectors)
    n = len(ids)
    if n == 0:
        return []
    unit = np.stack([vectors[i] for i in ids]).astype(np.float64)
    norms = np.linalg.norm(unit, axis=1, keepdims=True)
    unit = unit / np.where(norms == 0, 1.0, norms)
    sums = unit @ unit.T  # sum of pairwise similarities between clusters
    members: dict[int, list[int]] = {k: [ids[k]] for k in range(n)}
    while len(members) > 1:
        keys = sorted(members)
        best: tuple[float, int, int] | None = None
        for x, a in enumerate(keys):
            for b in keys[x + 1 :]:
                sim = float(sums[a, b]) / (len(members[a]) * len(members[b]))
                if best is None or sim > best[0] + 1e-12:
                    best = (sim, a, b)
        assert best is not None
        sim, a, b = best
        if sim < settings.topic_group_threshold and len(members) <= settings.topic_group_max:
            break
        members[a].extend(members.pop(b))
        sums[a, :] += sums[b, :]
        sums[:, a] += sums[:, b]
    return sorted((sorted(m) for m in members.values()), key=lambda m: m[0])


# --- persistence helpers ---


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


async def _story_vectors(session: AsyncSession, story_ids: Iterable[int]) -> dict[int, np.ndarray]:
    store = get_vector_store(session)
    found = await store.get_story_centroids(list(story_ids))
    return {sid: np.asarray(vec, dtype=np.float64) for sid, vec in found.items() if vec}


def _rule_usable(rule: TopicRule) -> bool:
    return not rule.embed_model or rule.embed_model == settings.embed_model


async def _saved_by_user(session: AsyncSession, story_id: int) -> set[int]:
    rows = await session.scalars(
        select(StoryState.user_id).where(
            StoryState.story_id == story_id, StoryState.saved_at.is_not(None)
        )
    )
    return set(rows.all())


async def evaluate_story(session: AsyncSession, story_id: int) -> None:
    """Fast path after clustering: attach the story to a topic and refresh rule matches.

    No LLM call. Caller commits. Never raises into the pipeline."""
    if not settings.topics_enabled:
        return
    vecs = await _story_vectors(session, [story_id])
    vec = vecs.get(story_id)
    if vec is None:
        return
    topics = (await session.scalars(select(Topic).where(Topic.status == "active"))).all()
    best: tuple[Topic | None, float] = (None, settings.topic_match_threshold)
    for topic in topics:
        centroid = decode(topic.centroid)
        if centroid.shape != vec.shape:
            continue
        sim = cosine_similarity(vec, centroid)
        if sim >= best[1]:
            best = (topic, sim)
    await session.execute(delete(StoryTopic).where(StoryTopic.story_id == story_id))
    if best[0] is not None:
        session.add(StoryTopic(story_id=story_id, topic_id=best[0].id, similarity=best[1]))
        best[0].last_story_at = utcnow()
    await _match_story_rules(session, story_id, vec)


async def _group_centroids(session: AsyncSession) -> dict[int, list[np.ndarray]]:
    """group id -> centroids of its current (active) member topics."""
    rows = (
        await session.execute(
            select(Topic.group_id, Topic.centroid).where(
                Topic.status == "active", Topic.group_id.is_not(None)
            )
        )
    ).all()
    found: dict[int, list[np.ndarray]] = {}
    for gid, blob in rows:
        found.setdefault(gid, []).append(decode(blob))
    return found


def _rule_centroids(rule: TopicRule, groups: dict[int, list[np.ndarray]]) -> list[np.ndarray]:
    """Centroids a rule matches against: the live members of its group (membership evolves
    with the groups), else its own snapshot (also when the group vanished)."""
    if rule.source_group_id is not None and groups.get(rule.source_group_id):
        return groups[rule.source_group_id]
    return [decode(rule.centroid)]


def _any_match(vec: np.ndarray, centroids: list[np.ndarray], threshold: float) -> bool:
    return any(rule_matches(vec, c, threshold) for c in centroids)


async def _match_story_rules(session: AsyncSession, story_id: int, vec: np.ndarray) -> None:
    await session.execute(delete(StoryRuleMatch).where(StoryRuleMatch.story_id == story_id))
    rules = (await session.scalars(select(TopicRule))).all()
    groups = await _group_centroids(session)
    for rule in rules:
        if _rule_usable(rule) and _any_match(vec, _rule_centroids(rule, groups), rule.threshold):
            session.add(
                StoryRuleMatch(
                    rule_id=rule.id, story_id=story_id, user_id=rule.user_id, mode=rule.mode
                )
            )


async def sync_rule_matches(session: AsyncSession, rule: TopicRule) -> int:
    """(Re)compute one rule's matches over stories updated within the scan window."""
    await session.execute(delete(StoryRuleMatch).where(StoryRuleMatch.rule_id == rule.id))
    if not _rule_usable(rule):
        return 0
    since = utcnow() - timedelta(days=settings.topic_rule_scan_days)
    ids = (await session.scalars(select(Story.id).where(Story.last_updated_at >= since))).all()
    centroids = _rule_centroids(rule, await _group_centroids(session))
    count = 0
    for sid, vec in (await _story_vectors(session, ids)).items():
        if _any_match(vec, centroids, rule.threshold):
            session.add(
                StoryRuleMatch(rule_id=rule.id, story_id=sid, user_id=rule.user_id, mode=rule.mode)
            )
            count += 1
    return count


async def reconcile_topic_rules(session: AsyncSession) -> int:
    """Refresh materialized semantic matches after changing the rule-matching contract."""
    count = 0
    for rule in (await session.scalars(select(TopicRule))).all():
        count += await sync_rule_matches(session, rule)
    await session.commit()
    return count


async def create_rule(
    session: AsyncSession,
    user_id: int,
    topic: Topic,
    mode: str,
    *,
    until_fade: bool,
    expires_at: datetime | None,
) -> TopicRule:
    existing = (
        await session.scalars(
            select(TopicRule).where(
                TopicRule.user_id == user_id, TopicRule.source_topic_id == topic.id
            )
        )
    ).all()
    for old in existing:  # one rule per user+topic: mute and follow replace each other
        await delete_rule(session, old)
    rule = TopicRule(
        user_id=user_id,
        mode=mode,
        label=topic.label,
        centroid=topic.centroid,
        embed_model=settings.embed_model,
        threshold=settings.topic_match_threshold,
        source_topic_id=topic.id,
        until_fade=until_fade,
        expires_at=expires_at,
    )
    session.add(rule)
    await session.flush()
    matched = await sync_rule_matches(session, rule)
    await activity.emit(
        session,
        "topics",
        "rule_created",
        {"rule_id": rule.id, "topic_id": topic.id, "mode": mode, "matched": matched},
    )
    return rule


async def create_group_rule(
    session: AsyncSession,
    user_id: int,
    group: TopicGroup,
    mode: str,
    *,
    until_fade: bool,
    expires_at: datetime | None,
) -> TopicRule:
    existing = (
        await session.scalars(
            select(TopicRule).where(
                TopicRule.user_id == user_id, TopicRule.source_group_id == group.id
            )
        )
    ).all()
    for old in existing:  # one rule per user+group
        await delete_rule(session, old)
    rule = TopicRule(
        user_id=user_id,
        mode=mode,
        label=group.label,
        centroid=group.centroid,
        embed_model=settings.embed_model,
        threshold=settings.topic_match_threshold,
        source_group_id=group.id,
        until_fade=until_fade,
        expires_at=expires_at,
    )
    session.add(rule)
    await session.flush()
    matched = await sync_rule_matches(session, rule)
    await activity.emit(
        session,
        "topics",
        "rule_created",
        {"rule_id": rule.id, "group_id": group.id, "mode": mode, "matched": matched},
    )
    return rule


class ManualTopicError(ValueError):
    """Typed-topic validation failure; ``code`` is machine-readable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def create_manual_rule(
    session: AsyncSession,
    user_id: int,
    query: str,
    mode: str,
    *,
    expires_at: datetime | None,
) -> TopicRule:
    """A topic the user typed: the phrase is embedded (``EMBED_MODEL``, any language) and
    becomes a rule with no source topic. Same phrase again replaces the previous rule."""
    label = " ".join(query.split())
    if not 2 <= len(label) <= 80:
        raise ManualTopicError("invalid_length")
    rules = (
        await session.scalars(select(TopicRule).where(TopicRule.user_id == user_id))
    ).all()
    manual = [r for r in rules if r.source_topic_id is None and r.source_group_id is None]
    for old in manual:
        if old.label.casefold() == label.casefold():
            await delete_rule(session, old)
            manual.remove(old)
    if len(manual) >= settings.topic_manual_max:
        raise ManualTopicError("too_many")
    vectors = await llm_client.embed([label])
    if not vectors or not vectors[0]:
        raise ManualTopicError("embedding_failed")
    rule = TopicRule(
        user_id=user_id,
        mode=mode,
        label=label,
        centroid=encode(vectors[0]),
        embed_model=settings.embed_model,
        threshold=settings.topic_match_threshold,
        source_topic_id=None,
        until_fade=False,
        expires_at=expires_at,
    )
    session.add(rule)
    await session.flush()
    matched = await sync_rule_matches(session, rule)
    await activity.emit(
        session, "topics", "rule_created", {"rule_id": rule.id, "manual": True, "matched": matched}
    )
    return rule


async def set_rule_mode(session: AsyncSession, rule: TopicRule, mode: str) -> None:
    rule.mode = mode
    await session.execute(
        update(StoryRuleMatch).where(StoryRuleMatch.rule_id == rule.id).values(mode=mode)
    )
    await session.flush()


async def set_aside_rule_matches(session: AsyncSession, rule: TopicRule) -> int:
    """Keep the stories a mute rule hides hidden once the rule is gone. Returns how many
    were newly set aside."""
    if rule.mode != MODE_MUTE:
        return 0
    story_ids = (
        await session.scalars(
            select(StoryRuleMatch.story_id).where(
                StoryRuleMatch.rule_id == rule.id, StoryRuleMatch.user_id == rule.user_id
            )
        )
    ).all()
    if not story_ids:
        return 0
    existing = set(
        (
            await session.scalars(
                select(StorySetAside.story_id).where(
                    StorySetAside.user_id == rule.user_id,
                    StorySetAside.story_id.in_(story_ids),
                )
            )
        ).all()
    )
    now = utcnow()
    fresh = [sid for sid in story_ids if sid not in existing]
    for sid in fresh:
        session.add(StorySetAside(user_id=rule.user_id, story_id=sid, set_aside_at=now))
    await session.flush()
    return len(fresh)


async def expire_rule(session: AsyncSession, rule: TopicRule) -> None:
    """End of life of a rule (date or "until the topic fades"): muted stories are set aside
    instead of reappearing as unread."""
    await set_aside_rule_matches(session, rule)
    await delete_rule(session, rule)


async def delete_rule(session: AsyncSession, rule: TopicRule) -> None:
    await session.execute(delete(StoryRuleMatch).where(StoryRuleMatch.rule_id == rule.id))
    await session.delete(rule)
    await session.flush()


async def preview_mute(
    session: AsyncSession, topic: Topic, user_feed_ids: set[int], user_id: int
) -> tuple[int, list[tuple[int, str]]]:
    """Unread stories (of this user) a rule built from ``topic`` would hide + sample titles."""
    return await _preview_hidden(session, [decode(topic.centroid)], user_feed_ids, user_id)


async def preview_group_mute(
    session: AsyncSession, group: TopicGroup, user_feed_ids: set[int], user_id: int
) -> tuple[int, list[tuple[int, str]]]:
    """Same as ``preview_mute`` for a group rule (any member topic centroid matches)."""
    centroids = (await _group_centroids(session)).get(group.id) or [decode(group.centroid)]
    return await _preview_hidden(session, centroids, user_feed_ids, user_id)


async def _preview_hidden(
    session: AsyncSession, centroids: list[np.ndarray], user_feed_ids: set[int], user_id: int
) -> tuple[int, list[tuple[int, str]]]:
    since = utcnow() - timedelta(days=settings.topic_rule_scan_days)
    visible = select(Article.story_id).where(
        Article.feed_id.in_(user_feed_ids), Article.story_id.is_not(None)
    )
    rows = (
        await session.execute(
            select(Story.id, Story.title, StoryState.is_read, StoryState.saved_at)
            .outerjoin(
                StoryState, (StoryState.story_id == Story.id) & (StoryState.user_id == user_id)
            )
            .where(
                Story.id.in_(visible),
                Story.last_updated_at >= since,
                func.coalesce(StoryState.is_read, False).is_(False),
                StoryState.saved_at.is_(None),
            )
            .order_by(Story.last_updated_at.desc())
        )
    ).all()
    vecs = await _story_vectors(session, [r[0] for r in rows])
    hidden = [
        (sid, title)
        for sid, title, _is_read, _saved_at in rows
        if sid in vecs and _any_match(vecs[sid], centroids, settings.topic_match_threshold)
    ]
    return len(hidden), hidden[:5]


# --- discovery sweep ---


async def _label_topic(
    session: AsyncSession, headlines: list[str], fallback: str
) -> tuple[str, str]:
    if not llm_client.is_configured():
        return fallback, ""
    try:
        system, user = prompts.topic_label(headlines[:6])
        with llmtrace.context("topic_label", label=fallback):
            result, latency_ms = await llm_client.chat_json(system, user)
        usage.record(
            session,
            "topic_label",
            endpoint="chat",
            model=settings.llm_model,
            latency_ms=latency_ms,
            prompt_chars=len(system) + len(user),
        )
        label = str(result.get("label") or "").strip()[:256]
        return (label or fallback), str(result.get("description") or "").strip()
    except llm_client.LLMError as exc:
        await activity.emit(session, "topics", "label_error", {"error": str(exc)}, level="error")
        return fallback, ""


_refresh_lock = asyncio.Lock()


async def refresh_topics(session: AsyncSession) -> dict[str, int]:
    """Discover new topics, refresh counts, fade stale topics, expire rules. Commits.

    Serialized: a first sweep labels every topic with the LLM and can take minutes, and two
    overlapping runs would each create the same topics.
    """
    async with _refresh_lock:
        report = await _refresh_topics(session)
        if settings.topics_enabled:
            report.update(await _refresh_groups(session))
        return report


async def _refresh_topics(session: AsyncSession) -> dict[str, int]:
    report = {"created": 0, "faded": 0, "rules_expired": 0, "assigned": 0}
    if not settings.topics_enabled:
        return report
    now = datetime.now(UTC)
    window = now - timedelta(days=settings.topic_window_days)
    stories = (
        await session.execute(
            select(Story.id, Story.title, Story.last_updated_at).where(
                Story.last_updated_at >= window
            )
        )
    ).all()
    titles = {sid: title for sid, title, _ in stories}
    vectors = await _story_vectors(session, titles)

    topics = (await session.scalars(select(Topic).where(Topic.status == "active"))).all()
    centroids = {t.id: decode(t.centroid) for t in topics}
    assignment: dict[int, tuple[int, float]] = {}
    unassigned: dict[int, np.ndarray] = {}
    for sid, vec in vectors.items():
        best, best_sim = None, settings.topic_match_threshold
        for tid, c in centroids.items():
            if c.shape != vec.shape:
                continue
            sim = cosine_similarity(vec, c)
            if sim >= best_sim:
                best, best_sim = tid, sim
        if best is None:
            unassigned[sid] = vec
        else:
            assignment[sid] = (best, best_sim)

    hosts: dict[int, set[str]] = {}
    for sid, url in (
        await session.execute(
            select(Article.story_id, Article.url).where(Article.story_id.in_(list(vectors)))
        )
    ).all():
        if sid is not None:
            hosts.setdefault(sid, set()).add(_host(url))

    for members_vec, members in cluster_vectors(unassigned, settings.topic_match_threshold):
        source_hosts = set().union(*(hosts.get(m, set()) for m in members))
        if (
            len(members) < settings.topic_min_stories
            or len(source_hosts) < settings.topic_min_sources
        ):
            continue
        central = max(members, key=lambda m: cosine_similarity(vectors[m], members_vec))
        label, description = await _label_topic(
            session, [titles[m] for m in members], titles[central]
        )
        topic = Topic(
            label=label,
            description=description,
            label_language=settings.summary_language,
            centroid=encode(members_vec),
            story_count=len(members),
            source_count=len(source_hosts),
            last_story_at=now,
        )
        session.add(topic)
        await session.flush()
        for m in members:
            assignment[m] = (topic.id, cosine_similarity(vectors[m], members_vec))
        report["created"] += 1
        await activity.emit(
            session,
            "topics",
            "topic_created",
            {"topic_id": topic.id, "label": label, "stories": len(members)},
        )

    # rewrite assignments for window stories, then recompute topic stats + centroids
    await session.execute(delete(StoryTopic).where(StoryTopic.story_id.in_(list(vectors))))
    for sid, (tid, sim) in assignment.items():
        session.add(StoryTopic(story_id=sid, topic_id=tid, similarity=sim))
    report["assigned"] = len(assignment)
    await session.flush()

    by_topic: dict[int, list[int]] = {}
    for sid, (tid, _) in assignment.items():
        by_topic.setdefault(tid, []).append(sid)
    last_update = {sid: ts for sid, _, ts in stories}
    for topic in (await session.scalars(select(Topic).where(Topic.status == "active"))).all():
        members = by_topic.get(topic.id, [])
        if members:
            topic.centroid = encode(np.mean([vectors[m] for m in members], axis=0))
            topic.story_count = len(members)
            topic.source_count = len(set().union(*(hosts.get(m, set()) for m in members)))
            topic.last_story_at = max(last_update[m] for m in members)
        last = topic.last_story_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now - last > timedelta(days=settings.topic_fade_days):
            topic.status = "faded"
            report["faded"] += 1
            await activity.emit(session, "topics", "topic_faded", {"topic_id": topic.id})
    await session.flush()

    # rule expiry: explicit date, or "until the source topic fades"
    faded_ids = set((await session.scalars(select(Topic.id).where(Topic.status == "faded"))).all())
    for rule in (await session.scalars(select(TopicRule))).all():
        expires = rule.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if (expires is not None and expires <= now) or (
            rule.until_fade and rule.source_topic_id in faded_ids
        ):
            await expire_rule(session, rule)
            report["rules_expired"] += 1
            await activity.emit(session, "topics", "rule_expired", {"rule_id": rule.id})
    await session.commit()
    return report


async def _label_group(
    session: AsyncSession, topic_labels: list[str], fallback: str
) -> tuple[str, str]:
    if not llm_client.is_configured():
        return fallback, ""
    try:
        system, user = prompts.topic_group_label(topic_labels[:12])
        with llmtrace.context("topic_label", label=fallback):
            result, latency_ms = await llm_client.chat_json(system, user)
        usage.record(
            session,
            "topic_label",
            endpoint="chat",
            model=settings.llm_model,
            latency_ms=latency_ms,
            prompt_chars=len(system) + len(user),
        )
        label = str(result.get("label") or "").strip()[:256]
        return (label or fallback), str(result.get("description") or "").strip()
    except llm_client.LLMError as exc:
        await activity.emit(session, "topics", "label_error", {"error": str(exc)}, level="error")
        return fallback, ""


GROUP_IDENTITY_SIMILARITY = 0.8


async def _refresh_groups(session: AsyncSession) -> dict[str, int]:
    """Regroup the active topics into galaxies, keeping ids/labels of groups that persist.

    Each step commits on its own so the SQLite write lock stays short (LLM labelling in
    between runs with no transaction open)."""
    report = {"groups_created": 0, "groups_deleted": 0, "groups_relabeled": 0}
    topics = list((await session.scalars(select(Topic).where(Topic.status == "active"))).all())
    dim = len(decode(topics[0].centroid)) if topics else 0
    by_id = {t.id: t for t in topics if len(decode(t.centroid)) == dim}
    vectors = {tid: decode(t.centroid) for tid, t in by_id.items()}
    plan = group_topics(vectors)
    group_vectors: list[np.ndarray] = []
    for members in plan:
        weights = np.array([max(by_id[m].story_count, 1) for m in members], dtype=np.float64)
        group_vectors.append(np.average([vectors[m] for m in members], axis=0, weights=weights))
    existing = {g.id: g for g in (await session.scalars(select(TopicGroup))).all()}
    old_members: dict[int, set[int]] = {}
    for t in topics:
        if t.group_id is not None:
            old_members.setdefault(t.group_id, set()).add(t.id)
    await session.commit()

    # greedy best-first identity matching on centroid cosine
    pairs = sorted(
        (
            (cosine_similarity(vec, decode(g.centroid)), i, old_id)
            for i, vec in enumerate(group_vectors)
            for old_id, g in existing.items()
            if decode(g.centroid).shape == vec.shape
        ),
        key=lambda p: (-p[0], p[1], p[2]),
    )
    matched: dict[int, int] = {}  # plan index -> existing group id
    taken: set[int] = set()
    for sim, i, old_id in pairs:
        if sim < GROUP_IDENTITY_SIMILARITY:
            break
        if i in matched or old_id in taken:
            continue
        matched[i] = old_id
        taken.add(old_id)

    assigned: dict[int, int] = {}  # topic id -> group id
    for i, members in enumerate(plan):
        member_topics = sorted((by_id[m] for m in members), key=lambda t: -t.story_count)
        labels = [t.label for t in member_topics]
        stories = sum(t.story_count for t in member_topics)
        found = matched.get(i)
        gid: int
        if found is None:
            label, description = await _label_group(session, labels, labels[0])
            group = TopicGroup(
                label=label,
                description=description,
                label_language=settings.summary_language,
                centroid=encode(group_vectors[i]),
                topic_count=len(members),
                story_count=stories,
            )
            session.add(group)
            await session.flush()
            gid = group.id
            report["groups_created"] += 1
            await activity.emit(
                session,
                "topics",
                "group_created",
                {"group_id": gid, "label": label, "topics": len(members)},
            )
        else:
            gid = found
            group = existing[gid]
            before = old_members.get(gid, set())
            union = before | set(members)
            changed = len(before ^ set(members)) / len(union) if union else 0.0
            if changed > 0.5:
                label, description = await _label_group(session, labels, labels[0])
                group.label, group.description = label, description
                group.label_language = settings.summary_language
                report["groups_relabeled"] += 1
                await activity.emit(
                    session, "topics", "group_relabeled", {"group_id": gid, "label": label}
                )
            group.centroid = encode(group_vectors[i])
            group.topic_count = len(members)
            group.story_count = stories
        for m in members:
            assigned[m] = gid
        await session.commit()

    for t in (await session.scalars(select(Topic).where(Topic.group_id.is_not(None)))).all():
        if assigned.get(t.id) != t.group_id:
            t.group_id = assigned.get(t.id)
    for tid, new_gid in assigned.items():
        topic = by_id[tid]
        if topic.group_id != new_gid:
            topic.group_id = new_gid
    await session.commit()

    keep = set(assigned.values())
    gone = [old_id for old_id in existing if old_id not in keep]
    if gone:
        await session.execute(delete(TopicGroup).where(TopicGroup.id.in_(gone)))
        report["groups_deleted"] = len(gone)
        for old_id in gone:
            await activity.emit(session, "topics", "group_deleted", {"group_id": old_id})
        # "until the topic fades" for a group rule = the group disappears
        for rule in (
            await session.scalars(
                select(TopicRule).where(
                    TopicRule.source_group_id.in_(gone), TopicRule.until_fade.is_(True)
                )
            )
        ).all():
            await expire_rule(session, rule)
            await activity.emit(session, "topics", "rule_expired", {"rule_id": rule.id})
        await session.commit()

    # membership moved: refresh the matches of every remaining group rule
    for rule in (
        await session.scalars(select(TopicRule).where(TopicRule.source_group_id.is_not(None)))
    ).all():
        await sync_rule_matches(session, rule)
        await session.commit()
    return report


async def story_radar(session: AsyncSession, story_id: int) -> dict[str, dict[str, object]] | None:
    """The active Radar topic a story belongs to, and that topic's galaxy, each with a 0..1 score.

    The topic score is the stored similarity to the topic centroid; the galaxy score is the story's
    similarity to the galaxy centroid. None when the story is in no active topic."""
    if not settings.topics_enabled:
        return None
    row = (
        await session.execute(
            select(StoryTopic.similarity, Topic)
            .join(Topic, Topic.id == StoryTopic.topic_id)
            .where(StoryTopic.story_id == story_id, Topic.status == "active")
        )
    ).first()
    if row is None:
        return None
    similarity, topic = row
    result: dict[str, dict[str, object]] = {
        "topic": {"id": topic.id, "label": topic.label, "score": round(float(similarity), 3)}
    }
    group = await session.get(TopicGroup, topic.group_id) if topic.group_id is not None else None
    if group is not None:
        score: float | None = None
        vec = (await _story_vectors(session, [story_id])).get(story_id)
        centroid = decode(group.centroid)
        if vec is not None and vec.shape == centroid.shape:
            score = round(float(cosine_similarity(vec, centroid)), 3)
        result["group"] = {"id": group.id, "label": group.label, "score": score}
    return result


async def story_visibility_map(
    session: AsyncSession, user_id: int, story_ids: list[int]
) -> dict[int, tuple[str, int | None]]:
    """story id -> (visible|ignored|followed, deciding rule id) for one user."""
    if not story_ids:
        return {}
    rows = (
        await session.execute(
            select(StoryRuleMatch.story_id, StoryRuleMatch.mode, StoryRuleMatch.rule_id).where(
                StoryRuleMatch.user_id == user_id, StoryRuleMatch.story_id.in_(story_ids)
            )
        )
    ).all()
    saved = set(
        (
            await session.scalars(
                select(StoryState.story_id).where(
                    StoryState.user_id == user_id,
                    StoryState.story_id.in_(story_ids),
                    StoryState.saved_at.is_not(None),
                )
            )
        ).all()
    )
    aside = set(
        (
            await session.scalars(
                select(StorySetAside.story_id).where(
                    StorySetAside.user_id == user_id, StorySetAside.story_id.in_(story_ids)
                )
            )
        ).all()
    )
    grouped: dict[int, list[tuple[str, int]]] = {sid: [] for sid in aside}
    for sid, mode, rid in rows:
        grouped.setdefault(sid, []).append((mode, rid))
    out: dict[int, tuple[str, int | None]] = {}
    for sid, items in grouped.items():
        state = visibility([m for m, _ in items], saved=sid in saved, set_aside=sid in aside)
        wanted = {IGNORED: MODE_MUTE, FOLLOWED: MODE_FOLLOW}.get(state)
        rule_id = next((rid for m, rid in items if m == wanted), None)
        out[sid] = (state, rule_id)
    return out
