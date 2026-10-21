"""Story presets ("Smart Views") of the current user: /api/me/presets*."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.db import get_session
from app.models import User, UserFeed
from app.services import story_presets as sp

router = APIRouter(prefix="/me/presets", tags=["presets"])


class PresetCriteriaOut(BaseModel):
    status: str
    feed_ids: list[int]
    category_ids: list[str]
    source_kinds: list[str]
    query: str | None
    max_age_days: int | None


class PresetOut(BaseModel):
    id: str
    kind: str
    label: str | None
    icon: str
    display: str
    enabled: bool
    position: int
    sort: str
    order: str
    show_count: bool
    criteria: PresetCriteriaOut


class PresetsOut(BaseModel):
    presets: list[PresetOut]
    last_preset_id: str | None
    compact: bool


def _invalid(exc: sp.PresetValidationError) -> HTTPException:
    return HTTPException(
        422,
        detail={"code": exc.code, "message": exc.message},
    )


async def user_feed_id_set(session: AsyncSession, user: User) -> set[int]:
    rows = await session.scalars(select(UserFeed.feed_id).where(UserFeed.user_id == user.id))
    return set(rows.all())


async def effective_presets(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The user's presets (defaults when none stored), feed ids the user cannot see dropped."""
    return sp.visible_presets(
        sp.load_presets(user.story_presets), await user_feed_id_set(session, user)
    )


async def _out(session: AsyncSession, user: User) -> PresetsOut:
    presets = await effective_presets(session, user)
    last = sp.normalize_last_preset_id(user.last_preset_id, presets)
    return PresetsOut(
        presets=[PresetOut.model_validate(p) for p in presets],
        last_preset_id=last,
        compact=bool(user.presets_compact),
    )


@router.get("")
async def get_presets(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> PresetsOut:
    return await _out(session, user)


@router.put("")
async def put_presets(
    body: dict[str, Any],
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PresetsOut:
    """Replace the whole list, ``last_preset_id`` and ``compact`` (validated, normalised)."""
    try:
        unknown = set(body) - {"presets", "last_preset_id", "compact"}
        if unknown or "presets" not in body:
            raise sp.PresetValidationError(
                "invalid_payload", "body takes presets, last_preset_id and compact"
            )
        presets = sp.validate_presets(body["presets"])
        last = sp.normalize_last_preset_id(body.get("last_preset_id"), presets)
        compact = sp.validate_compact(body.get("compact", False))
    except sp.PresetValidationError as exc:
        raise _invalid(exc) from exc
    user.story_presets = sp.dump_presets(presets)
    user.last_preset_id = last
    user.presets_compact = compact
    await session.commit()
    return await _out(session, user)


@router.post("/reset")
async def reset_presets(
    body: dict[str, Any],
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PresetsOut:
    """``{"scope": "builtin"}`` restores built-ins and keeps customs; ``"all"`` drops customs."""
    scope = body.get("scope")
    if set(body) != {"scope"} or scope not in ("builtin", "all"):
        raise _invalid(sp.PresetValidationError("invalid_scope", "scope must be builtin|all"))
    if scope == "all":
        user.story_presets = None
        user.last_preset_id = None
        user.presets_compact = False
    else:
        current = sp.load_presets(user.story_presets)
        restored = sp.reset_builtin(current)
        user.story_presets = sp.dump_presets(restored)
        user.last_preset_id = sp.normalize_last_preset_id(user.last_preset_id, restored)
    await session.commit()
    return await _out(session, user)
