"""Admin settings (SPEC §8): runtime-overridable values + LLM test connection.

Env vars provide defaults; rows in the SETTING table override them at runtime.
Only whitelisted keys are settable from the API.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_user
from app.core.config import settings as env_settings
from app.core.db import get_session
from app.models import Setting
from app.services import llm_client
from app.services.process import queue_depth

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(admin_user)])

# Whitelisted runtime-overridable keys (mapped to core.config attributes)
OVERRIDABLE = {
    "llm_base_url": str,
    "llm_model": str,
    "llm_api_key": str,
    "embed_base_url": str,
    "embed_model": str,
    "summary_language": str,
    "vector_backend": str,
    "qdrant_url": str,
    "qdrant_api_key": str,
    "tau_attach": float,
    "tau_gray": float,
    "freeze_after_hours": int,
    "retention_days": int,
    "feed_disable_after_days": int,
    "poll_interval_min_minutes": int,
    "poll_interval_max_minutes": int,
    "fulltext_min_chars": int,
}


def get_setting(stored: dict[str, str], key: str) -> object:
    """Stored override → env default. Applies the declared type."""
    cast = OVERRIDABLE[key]
    raw = stored.get(key)
    if raw is None:
        return getattr(env_settings, key)
    return cast(raw)


class SettingsOut(BaseModel):
    values: dict[str, object]
    overridden: list[str]
    llm_queue_depth: int


class SettingsPatch(BaseModel):
    values: dict[str, object]


@router.get("")
async def get_settings(session: AsyncSession = Depends(get_session)) -> SettingsOut:
    stored = await _load_overrides(session)
    values = {key: get_setting(stored, key) for key in OVERRIDABLE}
    return SettingsOut(
        values=values, overridden=sorted(stored), llm_queue_depth=queue_depth()
    )


@router.patch("")
async def patch_settings(
    body: SettingsPatch, session: AsyncSession = Depends(get_session)
) -> SettingsOut:
    for key, value in body.values.items():
        if key not in OVERRIDABLE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown setting: {key}")
        if value is None or value == "":
            await session.execute(delete(Setting).where(Setting.key == key))
            continue
        cast = OVERRIDABLE[key]
        try:
            cast(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Invalid value for {key}: {value!r}"
            ) from None
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=str(value)))
        else:
            row.value = str(value)
    await session.commit()
    _apply_overrides(await _load_overrides(session))
    return await get_settings(session)


@router.post("/test-llm")
async def test_llm(session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """Probe the configured LLM endpoints (chat + embeddings)."""
    _apply_overrides(await _load_overrides(session))
    result = await llm_client.test_connection()
    result["llm_base_url"] = env_settings.llm_base_url
    result["llm_model"] = env_settings.llm_model
    result["embed_model"] = env_settings.embed_model
    return result


@router.get("/threshold-report")
async def threshold_report(
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Offline threshold-tuning report (SPEC §5): precision/recall vs candidate τ,
    built from logged decisions + user corrections. Suggestion only — never
    auto-applied."""
    from app.services.feedback import threshold_report as build_report

    return await build_report(session)


async def _load_overrides(session: AsyncSession) -> dict[str, str]:
    rows = await session.scalars(select(Setting))
    return {r.key: r.value for r in rows if r.key in OVERRIDABLE}


def _apply_overrides(stored: dict[str, str]) -> None:
    """Push stored overrides into the live config object."""
    for key in OVERRIDABLE:
        cast = OVERRIDABLE[key]
        raw = stored.get(key)
        if raw is None:
            continue
        try:
            setattr(env_settings, key, cast(raw))
        except (TypeError, ValueError):
            continue


async def apply_overrides_at_startup(session: AsyncSession) -> None:
    _apply_overrides(await _load_overrides(session))
