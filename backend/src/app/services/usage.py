"""LLM usage metrics (SPEC §3 llm_usage).

One row per external LLM call, recorded by every pipeline/GUI call site right
after the chat_json/embed call. Token counts come from the OpenAI `usage`
object captured by llm_client into a task-local ContextVar; when the server
omits it (some local servers do), counts are estimated with a chars-per-token
heuristic and flagged `estimated=True` — the GUI surfaces a warning about
those rows.

Rows are appended to the caller's session and committed with the caller's
existing commit (every call site commits right after), so no extra round trip.
"""

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LLMUsage
from app.services import llm_client

if TYPE_CHECKING:
    from app.models import Article

# Rough chars-per-token heuristic for servers that omit `usage` (user decision:
# estimate with a visible warning rather than store nulls).
CHARS_PER_TOKEN = 4


def _est_tokens(chars: int) -> int | None:
    return max(chars // CHARS_PER_TOKEN, 1) if chars > 0 else None


def record(
    session: AsyncSession,
    kind: str,
    *,
    endpoint: str,
    model: str,
    latency_ms: int,
    article: "Article | None" = None,
    story_id: int | None = None,
    prompt_chars: int = 0,
    completion_chars: int = 0,
) -> None:
    """Append one usage row for the LLM call that just ran in this task.

    `article` denormalizes feed_id at insert time so per-source stats survive
    retention/feed deletion. Never raises — metrics must not break the pipeline.
    """
    try:
        u = llm_client.last_usage.get()
        estimated = u is None or u.get("total_tokens") is None
        if estimated:
            prompt = _est_tokens(prompt_chars)
            completion = _est_tokens(completion_chars)
            total = (prompt or 0) + (completion or 0) or None
            cached = reasoning = None
        else:
            assert u is not None
            prompt = u.get("prompt_tokens")
            completion = u.get("completion_tokens")
            total = u.get("total_tokens")
            cached = u.get("cached_tokens")
            reasoning = u.get("reasoning_tokens")
        session.add(
            LLMUsage(
                kind=kind,
                endpoint=endpoint,
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                cached_tokens=cached,
                reasoning_tokens=reasoning,
                estimated=estimated,
                latency_ms=latency_ms,
                article_id=article.id if article else None,
                story_id=story_id,
                feed_id=article.feed_id if article else None,
            )
        )
    except Exception:  # metrics must never break the pipeline
        pass
