"""LLM usage metrics API (admin only).

Backed by the append-only `llm_usage` table (one row per external LLM call).
Cost computation happens client-side on the Usage page — the prices are a
playground parameter there, not a server setting, so these endpoints return
raw token counts + latencies only. `tokens_per_s` is the throughput metric:
generation tokens/s for chat calls, prompt tokens/s for embeddings.

Rows flagged `estimated` come from servers that omit the OpenAI `usage`
object; their token counts are a chars-per-token heuristic, surfaced via
`estimated_calls` so the GUI can show a warning.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_user
from app.core.db import get_session
from app.models import Feed, LLMUsage, User

router = APIRouter(prefix="/usage", tags=["usage"])


def _sum(col: Any) -> Any:
    return func.coalesce(func.sum(col), 0)


def _period_start(period: str) -> datetime | None:
    now = datetime.now(UTC)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None  # "all"


def _sums() -> list[Any]:
    return [
        func.count(LLMUsage.id).label("calls"),
        _sum(LLMUsage.prompt_tokens).label("prompt_tokens"),
        _sum(LLMUsage.completion_tokens).label("completion_tokens"),
        _sum(LLMUsage.total_tokens).label("total_tokens"),
        _sum(LLMUsage.cached_tokens).label("cached_tokens"),
        _sum(LLMUsage.reasoning_tokens).label("reasoning_tokens"),
        _sum(LLMUsage.latency_ms).label("latency_ms"),
        _sum(func.cast(LLMUsage.estimated, Integer)).label("estimated_calls"),
    ]


def _tokens_per_s(endpoint: str, row: Any) -> float | None:
    """Throughput: completion tok/s for chat, prompt tok/s for embeddings."""
    if not row.latency_ms:
        return None
    tokens = row.prompt_tokens if endpoint == "embed" else row.completion_tokens
    return round(float(1000.0 * tokens / row.latency_ms), 1)


def _group_dict(row: Any, key: str, value: Any, endpoint: str) -> dict[str, Any]:
    return {
        key: value,
        "endpoint": endpoint,
        "calls": row.calls,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "total_tokens": row.total_tokens,
        "cached_tokens": row.cached_tokens,
        "reasoning_tokens": row.reasoning_tokens,
        "latency_ms": row.latency_ms,
        "estimated_calls": row.estimated_calls,
        "tokens_per_s": _tokens_per_s(endpoint, row),
    }


@router.get("/summary")
async def usage_summary(
    period: str = Query(default="all", pattern="^(day|month|all)$"),
    _admin: User = Depends(admin_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Totals for a period (UTC day / calendar month / full history),
    broken down by kind and by model."""
    start = _period_start(period)
    where = [LLMUsage.ts >= start] if start else []

    totals = (
        await session.execute(select(*_sums()).where(*where))
    ).one()

    by_kind_rows = (
        await session.execute(
            select(LLMUsage.kind, LLMUsage.endpoint, *_sums())
            .where(*where)
            .group_by(LLMUsage.kind, LLMUsage.endpoint)
            .order_by(_sum(LLMUsage.total_tokens).desc())
        )
    ).all()
    by_model_rows = (
        await session.execute(
            select(LLMUsage.model, LLMUsage.endpoint, *_sums())
            .where(*where)
            .group_by(LLMUsage.model, LLMUsage.endpoint)
            .order_by(_sum(LLMUsage.total_tokens).desc())
        )
    ).all()

    return {
        "period": period,
        "totals": {
            "calls": totals.calls,
            "prompt_tokens": totals.prompt_tokens,
            "completion_tokens": totals.completion_tokens,
            "total_tokens": totals.total_tokens,
            "cached_tokens": totals.cached_tokens,
            "reasoning_tokens": totals.reasoning_tokens,
            "estimated_calls": totals.estimated_calls,
        },
        "by_kind": [
            _group_dict(r, "kind", r.kind, r.endpoint) for r in by_kind_rows
        ],
        "by_model": [
            _group_dict(r, "model", r.model, r.endpoint) for r in by_model_rows
        ],
    }


@router.get("/daily")
async def usage_daily(
    days: int = Query(default=90, ge=1, le=1825),
    _admin: User = Depends(admin_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Per-day series (UTC) for the chart, split by kind."""
    start = datetime.now(UTC) - timedelta(days=days)
    day = func.date(LLMUsage.ts)
    rows = (
        await session.execute(
            select(
                day.label("day"),
                LLMUsage.kind,
                func.count(LLMUsage.id).label("calls"),
                _sum(LLMUsage.prompt_tokens).label("prompt_tokens"),
                _sum(LLMUsage.completion_tokens).label("completion_tokens"),
                _sum(LLMUsage.total_tokens).label("total_tokens"),
                _sum(LLMUsage.latency_ms).label("latency_ms"),
            )
            .where(LLMUsage.ts >= start)
            .group_by(day, LLMUsage.kind)
            .order_by(day)
        )
    ).all()
    return {
        "days": days,
        "rows": [
            {
                "day": r.day,
                "kind": r.kind,
                "calls": r.calls,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "latency_ms": r.latency_ms,
            }
            for r in rows
        ],
    }


@router.get("/by-feed")
async def usage_by_feed(
    _admin: User = Depends(admin_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Full-history totals per source feed (feed_id denormalized at insert, so
    stats survive retention; rows without an article have feed_id NULL)."""
    rows = (
        await session.execute(
            select(
                LLMUsage.feed_id,
                Feed.title,
                Feed.url,
                *_sums(),
            )
            .select_from(LLMUsage)
            .outerjoin(Feed, Feed.id == LLMUsage.feed_id)
            .group_by(LLMUsage.feed_id, Feed.title, Feed.url)
            .order_by(_sum(LLMUsage.total_tokens).desc())
        )
    ).all()
    return {
        "feeds": [
            {
                "feed_id": r.feed_id,
                # NULL title with a feed_id = feed deleted since
                "title": r.title or ("(deleted feed)" if r.feed_id else "(no source)"),
                "url": r.url,
                "calls": r.calls,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "estimated_calls": r.estimated_calls,
            }
            for r in rows
        ]
    }
