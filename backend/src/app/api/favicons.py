"""Source favicon proxy (cached) for story source logos.

Self-hosted: the GUI never hotlinks third-party favicon services. Auth is
required (cookie / Bearer / ?token=) — <img> tags cannot set headers, so the
GUI appends the session token when it has one.
"""

import re
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.api.deps import current_user
from app.core.config import settings
from app.models import User

router = APIRouter(prefix="/favicon", tags=["favicon"])

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", re.IGNORECASE)
_MAX_BYTES = 256 * 1024
_FAILURE_TTL_S = 3600  # don't re-hit a broken/slow site on every page load

# host -> (expires_at, content or None on failure, media type)
_cache: dict[str, tuple[float, bytes | None, str]] = {}


async def _fetch_favicon(host: str) -> tuple[bytes, str]:
    """Fetch https://<host>/favicon.ico. Module-level seam for tests."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=5.0) as client:
        resp = await client.get(f"https://{host}/favicon.ico")
        resp.raise_for_status()
        if len(resp.content) > _MAX_BYTES:
            raise ValueError("favicon too large")
        media = resp.headers.get("content-type", "image/x-icon").split(";")[0].strip()
        return resp.content, media


@router.get("")
async def favicon(
    host: str = Query(min_length=1, max_length=253),
    user: User = Depends(current_user),
) -> Response:
    if not _HOST_RE.match(host) or ".." in host:
        raise HTTPException(400, "Invalid host")
    host = host.lower()
    ttl = settings.favicon_cache_hours * 3600

    cached = _cache.get(host)
    if cached and cached[0] > time.time():
        if cached[1] is None:
            raise HTTPException(404, "No favicon")
        return _icon_response(cached[1], cached[2], ttl)

    try:
        content, media = await _fetch_favicon(host)
    except Exception:
        _cache[host] = (time.time() + _FAILURE_TTL_S, None, "")
        raise HTTPException(404, "No favicon") from None
    _cache[host] = (time.time() + ttl, content, media)
    return _icon_response(content, media, ttl)


def _icon_response(content: bytes, media: str, ttl: int) -> Response:
    return Response(
        content=content,
        media_type=media if media.startswith("image/") else "image/x-icon",
        headers={"Cache-Control": f"public, max-age={min(ttl, 86400)}"},
    )
