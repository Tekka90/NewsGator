"""Ops endpoints: health."""

from fastapi import APIRouter

from app.api.schemas import HealthOut

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> HealthOut:
    return HealthOut(status="ok", version="0.1.0")
