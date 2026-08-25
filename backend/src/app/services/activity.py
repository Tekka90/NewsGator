"""Activity events (SPEC §7, invariant 6).

Every pipeline stage transition must emit an event. Events are persisted to
ACTIVITY_LOG (ring buffer, capped) and broadcast live to SSE subscribers
(/activity/stream).
"""

import asyncio
import json
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityEvent

# SSE subscribers (in-process broadcast)
_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

RING_BUFFER_MAX = 5000


def subscribe() -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue[dict[str, Any]]) -> None:
    _subscribers.discard(q)


def _broadcast(payload: dict[str, Any]) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow consumer drops events rather than blocking the pipeline


def broadcast_queue(depth: int) -> None:
    """Push the LLM queue depth to SSE subscribers (SPEC §7)."""
    _broadcast({"action": "queue", "llm_queue_depth": depth})


async def emit(
    session: AsyncSession,
    component: str,
    action: str,
    detail: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Record + broadcast an activity event. Caller commits."""
    detail = detail or {}
    event = ActivityEvent(
        level=level,
        component=component,
        action=action,
        detail=json.dumps(detail, ensure_ascii=False),
    )
    session.add(event)
    _broadcast(
        {
            "level": level,
            "component": component,
            "action": action,
            "detail": detail,
        }
    )


async def prune(session: AsyncSession) -> None:
    """Keep ACTIVITY_LOG bounded (ring buffer)."""
    total = await session.scalar(select(func.count(ActivityEvent.id)))
    if total and total > RING_BUFFER_MAX:
        cutoff = await session.scalar(
            select(ActivityEvent.id)
            .order_by(ActivityEvent.id.desc())
            .offset(RING_BUFFER_MAX - 1)
            .limit(1)
        )
        if cutoff:
            await session.execute(delete(ActivityEvent).where(ActivityEvent.id < cutoff))
