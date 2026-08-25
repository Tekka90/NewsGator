"""Story clustering (SPEC §4/§5).

Per article (state 'embedded'):
  1. ANN search over NON-FROZEN story centroids
  2. similarity ≥ τ_attach → attach directly
     τ_gray..τ_attach   → LLM pairwise "same event?" confirmation
     below              → create a new story (+ LLM headline)
  3. Attaching to an existing story runs the novelty flow: version bumps ONLY on
     real content change (invariant 3); otherwise just last_updated_at.

Centroids are recency-weighted running means (24h half-life, SPEC §5), stored in
the vector store; every decision is logged for the threshold-tuning report (§5).
"""

from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Article, ClusterDecision, Story, StoryRevision
from app.services import activity, llm_client, prompts
from app.services.vectorstore import cosine_similarity, get_vector_store

HALF_LIFE_HOURS = 24.0


async def cluster_article(session: AsyncSession, article_id: int) -> None:
    """Cluster one embedded article. Idempotent: skips if already clustered."""
    article = await session.get(Article, article_id)
    if (
        article is None
        or article.processing_state != "embedded"
        or article.summary is None
    ):
        return

    vec = np.asarray(
        (await llm_client.embed([f"{article.title}\n\n{article.summary}"]))[0],
        dtype=np.float32,
    )
    store = get_vector_store(session)

    # Candidates from the store, then exclude frozen stories + exact cosine re-rank
    ranked: list[tuple[Story, float]] = []
    for story_id, _approx in await store.search_story_centroids(list(vec), limit=5):
        story = await session.get(Story, story_id)
        if story is None or story.is_frozen:
            continue
        centroid = await store.get_story_centroid(story.id)
        if centroid is None:
            continue
        ranked.append((story, cosine_similarity(vec, np.asarray(centroid))))
    ranked.sort(key=lambda t: t[1], reverse=True)

    decision = "new"
    matched: Story | None = None
    similarity: float | None = None

    if ranked:
        story, similarity = ranked[0]
        if similarity >= settings.tau_attach:
            matched, decision = story, "attach"
        elif similarity >= settings.tau_gray:
            matched, decision = await _gray_zone_check(session, story, article, similarity)

    if matched is not None:
        await _attach_to_story(session, matched, article, vec)
    else:
        matched = await _create_story(session, article, vec)

    article.story_id = matched.id
    article.processing_state = "clustered"
    session.add(
        ClusterDecision(
            article_id=article.id,
            story_id=matched.id,
            similarity=similarity,
            decision=decision,
        )
    )
    await activity.emit(
        session,
        "cluster",
        "cluster_attach" if decision.startswith("attach") else "cluster_new",
        {"article_id": article.id, "story_id": matched.id, "similarity": similarity},
    )
    await session.commit()


async def _gray_zone_check(
    session: AsyncSession, story: Story, article: Article, similarity: float
) -> tuple[Story | None, str]:
    """LLM 'same event?' confirmation in the gray zone (SPEC §4)."""
    assert article.summary is not None
    try:
        system, user = prompts.pairwise_same_event(story.summary, article.summary)
        result, latency_ms = await llm_client.chat_json(system, user)
        await activity.emit(
            session,
            "cluster",
            "pairwise_check",
            {
                "article_id": article.id,
                "story_id": story.id,
                "similarity": round(similarity, 4),
                "verdict": bool(result.get("same_event")),
                "llm_ms": latency_ms,
            },
        )
        if result.get("same_event"):
            return story, "attach_confirmed"
    except llm_client.LLMError as exc:
        await activity.emit(
            session,
            "cluster",
            "pairwise_error",
            {"article_id": article.id, "error": str(exc)},
            level="error",
        )
    return None, "new"


async def _create_story(
    session: AsyncSession, article: Article, vec: np.ndarray
) -> Story:
    """New story from a single article: LLM headline, version 1, centroid stored."""
    assert article.summary is not None
    story = Story(category=article.category or "Uncategorized")
    session.add(story)
    await session.flush()

    try:
        system, user = prompts.story_headline([article.summary])
        result, _ = await llm_client.chat_json(system, user)
        story.title = str(result.get("headline", "")) or article.title
    except llm_client.LLMError:
        story.title = article.title  # headline is cosmetic; fall back to article title

    story.summary = article.summary
    story.image_url = article.image_url
    session.add(StoryRevision(story_id=story.id, version=1, summary=story.summary))
    await get_vector_store(session).upsert_story_centroid(story.id, list(vec))
    return story


async def _attach_to_story(
    session: AsyncSession, story: Story, article: Article, vec: np.ndarray
) -> None:
    """Attach + novelty flow (SPEC §5): version bump ONLY on new facts."""
    assert article.summary is not None

    has_new_facts = True
    try:
        system, user = prompts.novelty_check(story.summary, article.summary)
        result, _ = await llm_client.chat_json(system, user)
        has_new_facts = bool(result.get("new_facts", True))
    except llm_client.LLMError:
        pass  # on LLM failure assume new facts (safer than losing updates)

    if has_new_facts:
        try:
            system, user = prompts.merge_story_summary(story.summary, article.summary)
            merged, _ = await llm_client.chat_json(system, user)
            story.summary = str(merged.get("summary") or story.summary)
            # Headline refresh: new facts may shift the story's angle
            story.title = str(merged.get("headline") or story.title)
        except llm_client.LLMError:
            pass  # keep old summary/title; version still bumps (new source with facts)
        story.version += 1
        session.add(
            StoryRevision(
                story_id=story.id, version=story.version, summary=story.summary
            )
        )
        await activity.emit(
            session, "cluster", "story_update",
            {"story_id": story.id, "version": story.version},
        )

    story.last_updated_at = datetime.now(UTC)
    if story.image_url is None and article.image_url:
        story.image_url = article.image_url  # backfill lead image
    await _update_centroid(session, story, vec)


async def _update_centroid(
    session: AsyncSession, story: Story, new_vec: np.ndarray
) -> None:
    """Recency-weighted running mean (24h half-life), stored back to the store."""
    store = get_vector_store(session)
    old_raw = await store.get_story_centroid(story.id)
    if old_raw is None:
        centroid = new_vec
    else:
        old = np.asarray(old_raw, dtype=np.float32)
        age_h = max(
            (datetime.now(UTC) - story.last_updated_at).total_seconds() / 3600.0, 0.0
        )
        w_old = 0.5 ** (age_h / HALF_LIFE_HOURS)
        centroid = (w_old * old + new_vec) / (w_old + 1.0)
    await store.upsert_story_centroid(story.id, centroid.astype(np.float32).tolist())


async def freeze_old_stories(session: AsyncSession) -> int:
    """Freeze stories older than FREEZE_AFTER_HOURS (SPEC §5 cluster aging)."""
    cutoff = datetime.now(UTC) - timedelta(hours=settings.freeze_after_hours)
    stories = (
        await session.scalars(
            select(Story).where(Story.is_frozen.is_(False), Story.first_seen_at < cutoff)
        )
    ).all()
    for story in stories:
        story.is_frozen = True
        await activity.emit(session, "cluster", "story_frozen", {"story_id": story.id})
    await session.commit()
    return len(stories)


# --- threshold-tuning feedback data (SPEC §5) — used by the M7 report ---


async def decisions_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(ClusterDecision.id))) or 0)
