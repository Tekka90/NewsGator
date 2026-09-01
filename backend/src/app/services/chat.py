"""Story chatbot (RAG over the story archive, SPEC §10).

Flow: embed the question with the same EMBED_MODEL used for articles (invariant
2) → ANN over story centroids → exact cosine re-rank (sqlite-vec's raw score is
a 1/(1+L2) proxy — never display it as-is) → ground the answer on the top-K
story summaries (already in SUMMARY_LANGUAGE, so the prompt satisfies invariant
1 for free). The LLM cites the stories it used by id; the GUI renders those as
clickable story cards.

Both LLM seams (_embed_query, _answer) are module-level for monkeypatching in
tests, like the other services.
"""

from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Article, Story
from app.services import activity, llm_client, prompts, usage
from app.services.vectorstore import cosine_similarity, get_vector_store


class ChatError(RuntimeError):
    pass


async def _embed_query(question: str) -> list[list[float]]:
    """Embedding seam (module-level for monkeypatching)."""
    return await llm_client.embed([question])


async def _answer(system: str, user: str) -> tuple[dict[str, Any], int]:
    """LLM answer seam (module-level for monkeypatching)."""
    return await llm_client.chat_json(system, user)


def is_enabled() -> bool:
    return bool(settings.chat_enabled)


async def _retrieve(
    session: AsyncSession, question_vec: list[float]
) -> tuple[list[Story], list[float], dict[int, float]]:
    """Top-`chat_top_k` stories: ANN then exact cosine re-rank (same pattern as
    api/stories._rank_candidates and cluster.py). Returns (stories, dropped
    candidate order, sim_by_id)."""
    store = get_vector_store(session)
    qv = np.asarray(question_vec, dtype=np.float32)
    scored: list[tuple[int, float]] = []
    for sid, _approx in await store.search_story_centroids(
        question_vec, limit=settings.chat_candidates
    ):
        centroid = await store.get_story_centroid(sid)
        if centroid is None:
            continue
        scored.append((sid, cosine_similarity(qv, np.asarray(centroid))))
    scored.sort(key=lambda t: t[1], reverse=True)
    top = scored[: settings.chat_top_k]
    stories: list[Story] = []
    sims: dict[int, float] = {}
    for sid, sim in top:
        story = await session.get(Story, sid)
        if story is None:  # centroid outlived a purged story
            continue
        stories.append(story)
        sims[sid] = sim
    return stories, [sim for _, sim in top], sims


async def _source_hosts(session: AsyncSession, story_ids: list[int]) -> dict[int, list[str]]:
    """story_id → distinct article hosts (≤5, www. stripped) — same as stories list."""
    from urllib.parse import urlparse

    if not story_ids:
        return {}
    rows = (
        await session.execute(
            select(Article.story_id, Article.url).where(Article.story_id.in_(story_ids))
        )
    ).all()
    hosts: dict[int, list[str]] = {}
    for sid, url in rows:
        if sid is None:
            continue
        host = urlparse(url).netloc.removeprefix("www.")
        if not host:
            continue
        lst = hosts.setdefault(sid, [])
        if host not in lst and len(lst) < 5:
            lst.append(host)
    return hosts


async def ask(
    session: AsyncSession,
    question: str,
    *,
    user_id: int,
    username: str,
) -> dict[str, Any]:
    """Answer `question` from the story archive. Emits chat_query activity events
    and records usage (chat_embed / chat_answer). Raises ChatError on failure."""
    if not is_enabled():
        raise ChatError("Chat is disabled")
    question = question.strip()
    if not question:
        raise ChatError("Empty question")

    await activity.emit(
        session,
        "chat",
        "chat_query",
        {"phase": "start", "user": username, "question": question[:200]},
    )
    await session.commit()

    # 1. Embed the question (same model as articles — invariant 2)
    vectors = await _embed_query(question)
    usage.record(
        session,
        "chat_embed",
        endpoint=settings.embed_base_url or settings.llm_base_url,
        model=settings.embed_model,
        latency_ms=0,
        prompt_chars=len(question),
    )
    if not vectors or not vectors[0]:
        await activity.emit(
            session, "chat", "chat_query", {"phase": "failed", "reason": "embed"},
            level="error",
        )
        await session.commit()
        raise ChatError("Embedding failed")

    # 2. Retrieve
    stories, _order, sims = await _retrieve(session, vectors[0])

    # 3. Answer (grounded)
    latency_ms = 0
    cited_ids: list[int] = []
    if not stories:
        answer = (
            "I couldn't find any stories matching that question in your news "
            "archive. Try rephrasing, or ask about a more recent or broader topic."
        )
    else:
        blocks = [
            (
                s.id,
                s.title,
                s.summary,
                s.category,
                s.last_updated_at.date().isoformat(),
            )
            for s in stories
        ]
        system, user_prompt = prompts.chat_answer(question, blocks)
        try:
            data, latency_ms = await _answer(system, user_prompt)
        except Exception as exc:
            await activity.emit(
                session, "chat", "chat_query",
                {"phase": "failed", "reason": str(exc)[:200]}, level="error",
            )
            await session.commit()
            raise ChatError(f"LLM answer failed: {exc}") from exc
        answer_raw = data.get("answer")
        if not (isinstance(answer_raw, str) and answer_raw.strip()):
            await activity.emit(
                session, "chat", "chat_query", {"phase": "failed", "reason": "empty"},
                level="error",
            )
            await session.commit()
            raise ChatError("LLM returned an empty answer")
        answer = answer_raw.strip()
        raw_ids = data.get("story_ids")
        if isinstance(raw_ids, list):
            valid = {s.id for s in stories}
            cited_ids = [int(i) for i in raw_ids if isinstance(i, int) and i in valid]
        usage.record(
            session,
            "chat_answer",
            endpoint=settings.llm_base_url,
            model=settings.llm_model,
            latency_ms=latency_ms,
            prompt_chars=len(system) + len(user_prompt),
            completion_chars=len(answer),
        )

    # 4. Citations: cited stories first, then the rest of the retrieved set
    ordered = stories if not cited_ids else sorted(
        stories, key=lambda s: (s.id not in cited_ids, stories.index(s))
    )
    hosts = await _source_hosts(session, [s.id for s in ordered])
    out_stories = [
        {
            "id": s.id,
            "title": s.title,
            "category": s.category,
            "image_url": s.image_url,
            "last_updated_at": s.last_updated_at,
            "source_hosts": hosts.get(s.id, []),
            "similarity": sims.get(s.id),
            "cited": s.id in cited_ids,
        }
        for s in ordered
    ]

    await activity.emit(
        session,
        "chat",
        "chat_query",
        {
            "phase": "done",
            "user": username,
            "stories": len(stories),
            "cited": len(cited_ids),
            "latency_ms": latency_ms,
        },
    )
    await session.commit()
    return {
        "answer": answer,
        "stories": out_stories,
        "latency_ms": latency_ms,
    }
