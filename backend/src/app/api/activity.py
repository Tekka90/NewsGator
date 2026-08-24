"""Activity API (SPEC §7): live SSE stream + recent history."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.db import get_session
from app.models import ActivityEvent, User
from app.services import activity
from app.services.process import queue_depth

router = APIRouter(prefix="/activity", tags=["activity"])


class EventOut(BaseModel):
    ts: datetime
    level: str
    component: str
    action: str
    detail: dict[str, object]


@router.get("/recent")
async def recent(
    limit: int = 200,
    component: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    limit = min(limit, 500)
    q = select(ActivityEvent).order_by(desc(ActivityEvent.id)).limit(limit)
    if component:
        q = q.where(ActivityEvent.component == component)
    rows = (await session.scalars(q)).all()
    return {
        "events": [
            EventOut(
                ts=e.ts,
                level=e.level,
                component=e.component,
                action=e.action,
                detail=json.loads(e.detail),
            )
            for e in reversed(rows)
        ],
        "llm_queue_depth": queue_depth(),
    }


@router.get("/stream")
async def stream(user: User = Depends(current_user)) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        q = activity.subscribe()
        try:
            yield f"event: hello\ndata: {json.dumps({'llm_queue_depth': queue_depth()})}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=25)
                    payload["ts"] = datetime.now(UTC).isoformat()
                except TimeoutError:
                    payload = {"action": "ping"}  # keep-alive
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            activity.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
