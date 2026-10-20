"""Map-reduce condensation of article text that does not fit the model context.

Used by `process.summarize_article` for EVERY article kind. Text that fits the budget is sent
whole (single call, no behaviour change); otherwise it is split on paragraphs, each chunk is
condensed into a digest of facts, and the digests replace the text. A second pass runs when the
digests still do not fit; as a last resort the digests are sampled evenly across the text.
Total LLM calls per article stay <= llm_budget.MAX_CALLS_PER_ARTICLE (the final summary
call included).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Article
from app.services import activity, llm_budget, llm_client, llmtrace, prompts, usage

# Rough characters per word used to turn a digest size target into a word limit
CHARS_PER_WORD = 6
MIN_DIGEST_WORDS = 100
MAX_DIGEST_WORDS = 1500


class CondenseError(Exception):
    """A condense call failed; the article stays retryable."""


async def _condense_chunks(
    session: AsyncSession,
    article: Article,
    chunks: list[str],
    budget_chars: int,
    calls_left: int,
) -> tuple[list[str], int]:
    """One map pass. Returns (digests, calls_made). Chunks beyond `calls_left` are sampled
    evenly across the text, never dropped from the end."""
    if len(chunks) > calls_left:
        chunks = llm_budget.sample_evenly(chunks, calls_left)
    words = min(
        max(budget_chars // max(len(chunks), 1) // CHARS_PER_WORD, MIN_DIGEST_WORDS),
        MAX_DIGEST_WORDS,
    )
    digests: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        system, user = prompts.condense_chunk(chunk, words)
        try:
            with llmtrace.context(
                "condense",
                label=f"{article.title[:60]} ({index}/{len(chunks)})",
                article_id=article.id,
            ):
                result, latency_ms = await llm_client.chat_json(system, user)
        except llm_client.LLMError as exc:
            await activity.emit(
                session,
                "llm",
                "condense_error",
                {
                    "article_id": article.id,
                    "chunk": index,
                    "chunks": len(chunks),
                    "error": str(exc),
                },
                level="error",
            )
            await session.commit()
            raise CondenseError(str(exc)) from exc
        digest = str(result.get("digest", "")).strip()
        usage.record(
            session,
            "condense",
            endpoint="chat",
            model=settings.llm_model,
            latency_ms=latency_ms,
            article=article,
            prompt_chars=len(system) + len(user),
            completion_chars=len(digest),
        )
        if digest:
            digests.append(digest)
    return digests, len(chunks)


async def fit_text(
    session: AsyncSession, article: Article, text: str, system: str, fixed_user_text: str
) -> str:
    """Return `text` unchanged when it fits the budget, else its map-reduce condensation.

    Raises CondenseError when a condense call fails.
    """
    budget = llm_budget.input_budget_chars(system, fixed_user_text)
    if llm_budget.fits(text, budget):
        return text

    condense_system, condense_fixed = prompts.condense_chunk("", MAX_DIGEST_WORDS)
    chunk_budget = llm_budget.input_budget_chars(condense_system, condense_fixed)
    chunk_size = max(chunk_budget // 2, llm_budget.MIN_BUDGET_CHARS // 2)
    calls_left = llm_budget.MAX_CALLS_PER_ARTICLE - 1  # one call is kept for the summary itself

    chunks = llm_budget.split_paragraphs(text, chunk_size)
    await activity.emit(
        session,
        "llm",
        "condense_start",
        {
            "article_id": article.id,
            "chars": len(text),
            "budget_chars": budget,
            "chunks": len(chunks),
        },
    )
    await session.commit()

    calls = 0
    passes = 1
    digests, made = await _condense_chunks(session, article, chunks, budget, calls_left)
    calls += made
    joined = "\n\n".join(digests)
    if not llm_budget.fits(joined, budget) and calls < calls_left:
        passes = 2
        second = llm_budget.split_paragraphs(joined, chunk_size)
        digests, made = await _condense_chunks(session, article, second, budget, calls_left - calls)
        calls += made
        joined = "\n\n".join(digests)
    sampled = not llm_budget.fits(joined, budget)
    if sampled:
        joined = llm_budget.sample_text(joined, budget)

    await activity.emit(
        session,
        "llm",
        "condense_done",
        {
            "article_id": article.id,
            "chunks": len(chunks),
            "calls": calls,
            "passes": passes,
            "digest_chars": len(joined),
            "sampled": sampled,
        },
    )
    await session.commit()
    return joined or text[:budget]
