"""Ops endpoints: health + stats."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.schemas import HealthOut
from app.core.db import get_session
from app.models import Article, Feed, Story, User
from app.services.process import queue_depth

router = APIRouter(tags=["ops"])

# Capabilities clients can probe to tell whether the running server is recent enough.
FEATURES = ["story_history"]
STARTED_AT = datetime.now(UTC).isoformat(timespec="seconds")


def _commit() -> str:
    """Build commit: GIT_COMMIT (containers) else the checkout's HEAD, else "unknown"."""
    if env := os.environ.get("GIT_COMMIT"):
        return env[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


COMMIT = _commit()


@router.get("/health")
async def health() -> HealthOut:
    return HealthOut(
        status="ok",
        version="0.1.0",
        commit=COMMIT,
        started_at=STARTED_AT,
        features=FEATURES,
    )


@router.get("/stats")
async def stats(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    return {
        "feeds": int(await session.scalar(select(func.count(Feed.id))) or 0),
        "articles": int(await session.scalar(select(func.count(Article.id))) or 0),
        "stories": int(await session.scalar(select(func.count(Story.id))) or 0),
        "llm_queue_depth": queue_depth(),
    }
