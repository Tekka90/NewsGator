"""Activity events (SPEC §7, invariant 6).

Every pipeline stage transition must emit an event. Milestone 2 persists to
ACTIVITY_LOG; the SSE stream (/activity/stream) is wired in Milestone 6 and reads
from the same table/broadcaster.
"""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityEvent


async def emit(
    session: AsyncSession,
    component: str,
    action: str,
    detail: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Record an activity event. Caller is responsible for committing."""
    session.add(
        ActivityEvent(
            level=level,
            component=component,
            action=action,
            detail=json.dumps(detail or {}, ensure_ascii=False),
        )
    )
