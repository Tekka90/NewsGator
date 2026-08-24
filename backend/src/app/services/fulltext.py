"""Full-text fetch chain (SPEC §9, invariant 8).

Order — stop at first success:
  1. direct fetch of the source URL (with per-feed cookies if configured)
  2. archive.is (https://archive.is/newest/<url>)
  3. RSS excerpt → content_status=partial + content_warning

Polite crawling: per-domain rate limit, robots.txt respected, archive.is failures
cached for `archive_failure_cache_hours`. All extraction runs via anyio.to_thread.
"""

import time
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import anyio
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Article, Feed
from app.services import activity

# Coarse markers — good enough to route to the next fallback (SPEC §9)
PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscription required",
    "this content is for subscribers",
    "create a free account to read",
    "already a subscriber",
    "sign in to continue reading",
)

USER_AGENT = "NewsGator/0.1 (+self-hosted feed reader)"
MIN_INTERVAL_S = 2.0  # per-domain rate limit

_robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
_domain_last_fetch: dict[str, float] = {}
_archive_failures: dict[str, float] = {}  # url → epoch of failure


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


async def _rate_limit(url: str) -> None:
    domain = _domain(url)
    last = _domain_last_fetch.get(domain, 0.0)
    wait = MIN_INTERVAL_S - (time.monotonic() - last)
    if wait > 0:
        await anyio.sleep(wait)
    _domain_last_fetch[domain] = time.monotonic()


def _robots_allowed_sync(url: str) -> bool:
    domain = _domain(url)
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{domain}/robots.txt"
    cached = _robots_cache.get(domain)
    if cached and time.monotonic() - cached[1] < 3600:
        rp = cached[0]
    else:
        rp = RobotFileParser(robots_url)
        try:
            rp.read()
        except Exception:
            return True  # unreachable robots.txt → allowed
        _robots_cache[domain] = (rp, time.monotonic())
    return rp.can_fetch(USER_AGENT, url)


async def _fetch_page(url: str, cookies: dict[str, str] | None = None) -> str | None:
    """HTTP GET → HTML, or None on failure. Isolated for testability."""
    await _rate_limit(url)
    allowed = await anyio.to_thread.run_sync(_robots_allowed_sync, url)
    if not allowed:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, cookies=cookies)
        if resp.status_code >= 400:
            return None
        return resp.text
    except httpx.HTTPError:
        return None


def _extract_text(html: str) -> str | None:
    """Blocking extraction (trafilatura → readability fallback)."""
    import trafilatura

    text = trafilatura.extract(html)
    if text:
        return text
    try:
        from readability import Document

        doc = Document(html)
        import trafilatura as t2

        return t2.extract(doc.summary())
    except Exception:
        return None


def _looks_paywalled(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PAYWALL_MARKERS)


def _parse_cookies(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    cookies: dict[str, str] = {}
    for pair in raw.split(";"):
        if "=" in pair:
            k, _, v = pair.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies or None


def _archive_url(url: str) -> str:
    return f"https://archive.is/newest/{quote(url, safe='')}"


async def fetch_full_text(session: AsyncSession, article: Article, feed: Feed) -> None:
    """Run the fetch chain for one article; persist outcome immediately."""
    cookies = _parse_cookies(feed.auth_cookies)
    path = "rss_only"
    text: str | None = None

    # 1. direct
    html = await _fetch_page(article.url, cookies=cookies)
    if html:
        candidate = await anyio.to_thread.run_sync(_extract_text, html)
        if candidate and len(candidate) >= settings.fulltext_min_chars and not _looks_paywalled(
            candidate
        ):
            text, path = candidate, "direct"

    # 2. archive.is (with per-URL failure cache)
    if text is None:
        failed_at = _archive_failures.get(article.url)
        cache_ok = failed_at is not None and (
            time.time() - failed_at < settings.archive_failure_cache_hours * 3600
        )
        if not cache_ok:
            archived = await _fetch_page(_archive_url(article.url))
            if archived:
                candidate = await anyio.to_thread.run_sync(_extract_text, archived)
                if candidate and len(candidate) >= settings.fulltext_min_chars:
                    text, path = candidate, "archive.is"
                else:
                    _archive_failures[article.url] = time.time()
            else:
                _archive_failures[article.url] = time.time()

    # 3. fallback to RSS excerpt
    if text is None:
        text = article.raw_content
        article.content_status = "partial"
        # SPEC §9: visible warning so partial summaries are marked in the story view
        article.content_warning = (
            "source does not provide full articles; requires credentials"
        )
    else:
        article.content_status = "full"

    article.full_text = text
    article.processing_state = "fulltext"
    await activity.emit(
        session,
        "fulltext",
        "fulltext_fetch",
        {"article_id": article.id, "path": path, "chars": len(text or "")},
        level="info" if path != "rss_only" else "warn",
    )
