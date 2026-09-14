"""Category-suggestion aggregation.

When the LLM feels no existing taxonomy category fits an article well, it may
propose a new one (`services/prompts.py::summarize_article`). Suggestions are
logged per-article (never auto-applied — see `record_suggestion`, called from
`services/process.py::summarize_article`) and aggregated here so an admin can
see recurring proposals (Settings page) and decide whether to add one to the
taxonomy.

Grouping is deliberately NOT semantic yet: `normalize()` only lowercases and
strips punctuation/whitespace, so "Apple", "apple." and " Apple " collapse but
"Apple" and "Apple Inc." remain distinct proposals. A follow-up could cluster
normalized texts by EMBED_MODEL cosine similarity (invariant 2's embedding
infra) before counting; out of scope for the initial version.
"""

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, CategoryProposalDismissal, CategorySuggestion

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    cleaned = _PUNCT_RE.sub("", text.lower())
    return _SPACE_RE.sub(" ", cleaned).strip()


async def record_suggestion(
    session: AsyncSession, article_id: int, raw_text: str, taxonomy: list[str]
) -> None:
    """Log a suggested new category unless it's blank or already in the taxonomy."""
    raw_text = raw_text.strip()[:128]
    if not raw_text:
        return
    normalized = normalize(raw_text)
    if not normalized or normalized in {normalize(t) for t in taxonomy}:
        return
    session.add(
        CategorySuggestion(article_id=article_id, raw_text=raw_text, normalized_text=normalized)
    )


class CategoryProposal(BaseModel):
    normalized_text: str
    label: str  # most frequent raw_text within the group — used for display + accept
    article_count: int
    first_seen: datetime
    last_seen: datetime
    example_titles: list[str]


async def list_proposals(
    session: AsyncSession, *, min_articles: int, window_days: int
) -> list[CategoryProposal]:
    """Recurring suggestions (>= min_articles distinct articles, within the
    window, not previously dismissed), most-suggested first."""
    since = datetime.now(UTC) - timedelta(days=window_days)
    rows = (
        await session.execute(
            select(
                CategorySuggestion.normalized_text,
                CategorySuggestion.raw_text,
                CategorySuggestion.article_id,
                CategorySuggestion.created_at,
            ).where(CategorySuggestion.created_at >= since)
        )
    ).all()
    if not rows:
        return []

    dismissed = set(
        (await session.scalars(select(CategoryProposalDismissal.normalized_text))).all()
    )

    grouped: dict[str, list[tuple[str, int, datetime]]] = defaultdict(list)
    for normalized, raw, article_id, created in rows:
        if normalized in dismissed:
            continue
        grouped[normalized].append((raw, article_id, created))

    proposals = []
    for normalized, items in grouped.items():
        article_ids = {aid for _, aid, _ in items}
        if len(article_ids) < min_articles:
            continue
        label = Counter(raw for raw, _, _ in items).most_common(1)[0][0]
        example_ids = list(article_ids)[:3]
        titles = (
            await session.scalars(select(Article.title).where(Article.id.in_(example_ids)))
        ).all()
        proposals.append(
            CategoryProposal(
                normalized_text=normalized,
                label=label,
                article_count=len(article_ids),
                first_seen=min(c for _, _, c in items),
                last_seen=max(c for _, _, c in items),
                example_titles=list(titles),
            )
        )
    proposals.sort(key=lambda p: p.article_count, reverse=True)
    return proposals


async def dismiss(session: AsyncSession, normalized_text: str) -> None:
    """Suppress this cluster from future proposals (does not delete the log)."""
    existing = await session.get(CategoryProposalDismissal, normalized_text)
    if existing is None:
        session.add(CategoryProposalDismissal(normalized_text=normalized_text))
    await session.commit()


async def clear_suggestions(session: AsyncSession, normalized_text: str) -> None:
    """Purge logged suggestions for a cluster once accepted into the taxonomy —
    it's now a real category, so record_suggestion won't re-log it anyway; this
    just keeps the log tidy."""
    await session.execute(
        delete(CategorySuggestion).where(CategorySuggestion.normalized_text == normalized_text)
    )
    await session.commit()
