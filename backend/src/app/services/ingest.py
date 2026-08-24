"""Feed ingestion (SPEC §9, pipeline stages fetched/fulltext).

Blocking work (feedparser) runs via anyio.to_thread. Every stage emits activity
events and persists article state immediately.
"""

import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import anyio
import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Article, Feed
from app.services import activity
from app.services.fulltext import fetch_full_text

TRACKING_PARAMS_PREFIXES = ("utm_",)
TRACKING_PARAMS_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref"}

USER_AGENT = "NewsGator/0.1 (+self-hosted feed reader)"


def canonicalize_url(url: str) -> str:
    """Strip tracking params and normalize — used for cross-feed dedupe (SPEC §9)."""
    parts = urlparse(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.startswith(TRACKING_PARAMS_PREFIXES) and k not in TRACKING_PARAMS_EXACT
    ]
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    return urlunparse((scheme, netloc, parts.path, "", urlencode(query), ""))


async def _http_get(feed: Feed) -> tuple[int, bytes, dict[str, str]]:
    """Fetch feed bytes honoring ETag/Last-Modified. Returns (status, body, headers).

    Isolated for testability (tests monkeypatch this).
    """
    headers = {"User-Agent": USER_AGENT}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(feed.url, headers=headers)
    return resp.status_code, resp.content, dict(resp.headers)


def _parse_entries(content: bytes) -> tuple[str, list[feedparser.FeedParserDict]]:
    """Blocking feedparser — runs in a thread via caller."""
    parsed = feedparser.parse(content)
    return str(parsed.feed.get("title", "")), list(parsed.entries)


def _entry_guid(entry: feedparser.FeedParserDict) -> str:
    return str(entry.get("id") or entry.get("link") or entry.get("title", ""))


def _entry_published(entry: feedparser.FeedParserDict) -> datetime | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
        struct = entry.get(f"{key}_parsed")
        if struct:
            return datetime.fromtimestamp(time.mktime(struct), tz=UTC)
    return None


def effective_interval_min(feed: Feed) -> int:
    """Adaptive polling (SPEC §9): slow down on empty polls and on failures."""
    cfg = settings
    base: int = feed.poll_interval_min or 30
    failures: int = feed.consecutive_failures or 0
    if failures > 0:
        # Exponential backoff, capped ~12h
        backoff = base * (2 ** min(failures, 6))
        return int(min(backoff, 12 * 60))
    # No new items → double interval up to the configured max
    empty_factor = 2 ** min(feed.empty_polls or 0, 3)
    return int(min(base * empty_factor, cfg.poll_interval_max_minutes))


def is_due(feed: Feed, now: datetime) -> bool:
    if not feed.is_enabled:
        return False
    if feed.last_fetched_at is None:
        return True
    return feed.last_fetched_at + timedelta(minutes=effective_interval_min(feed)) <= now


async def poll_feed(session: AsyncSession, feed: Feed) -> int:
    """Poll one feed: fetch, dedupe, store new articles, fetch full text.

    Returns number of new articles. All state persisted per-stage; failures update
    the failure policy counters (SPEC §9) and never raise to the scheduler.
    """
    await activity.emit(session, "ingest", "feed_poll_start", {"feed": feed.title or feed.url})
    await session.commit()
    try:
        new_count = await _poll_feed_inner(session, feed)
    except Exception as exc:
        await _record_failure(session, feed, exc)
        return 0

    feed.last_fetched_at = datetime.now(UTC)
    feed.consecutive_failures = 0
    feed.first_failure_at = None
    feed.last_error = None
    feed.empty_polls = 0 if new_count > 0 else feed.empty_polls + 1
    await activity.emit(
        session,
        "ingest",
        "feed_poll_done",
        {"feed": feed.title or feed.url, "new_articles": new_count},
    )
    await session.commit()
    return new_count


async def _poll_feed_inner(session: AsyncSession, feed: Feed) -> int:
    status_code, content, headers = await _http_get(feed)

    if status_code == 304:  # not modified
        return 0
    if status_code >= 400:
        raise RuntimeError(f"HTTP {status_code}")

    feed.etag = headers.get("etag")
    feed.last_modified = headers.get("last-modified")

    title, entries = await anyio.to_thread.run_sync(_parse_entries, content)
    if title and not feed.title:
        feed.title = title

    new_count = 0
    for entry in entries:
        guid = _entry_guid(entry)
        link = str(entry.get("link", ""))
        if not guid or not link:
            continue
        if await _is_duplicate(session, feed, guid, link):
            continue

        raw_content = ""
        if entry.get("content"):
            raw_content = str(entry.content[0].get("value", ""))
        elif entry.get("summary"):
            raw_content = str(entry.summary)

        article = Article(
            feed_id=feed.id,
            guid=guid,
            url=canonicalize_url(link),
            title=str(entry.get("title", "")),
            raw_content=raw_content,
            published_at=_entry_published(entry),
            processing_state="fetched",
        )
        session.add(article)
        await session.flush()  # assign id before fulltext stage

        if feed.fetch_fulltext:
            await fetch_full_text(session, article, feed)
        else:
            article.processing_state = "fulltext"
        # Hand off to the LLM queue (summarize → embed; cluster in M4)
        if article.processing_state == "fulltext":
            from app.services.process import enqueue_article

            enqueue_article(article.id)
        new_count += 1

    await session.commit()
    return new_count


async def _is_duplicate(session: AsyncSession, feed: Feed, guid: str, link: str) -> bool:
    """Dedupe layers (SPEC §9): (feed_id, guid) then canonical URL."""
    by_guid = await session.scalar(
        select(Article.id).where(Article.feed_id == feed.id, Article.guid == guid)
    )
    if by_guid is not None:
        return True
    canonical = canonicalize_url(link)
    by_url = await session.scalar(select(Article.id).where(Article.url == canonical))
    return by_url is not None


async def _record_failure(session: AsyncSession, feed: Feed, exc: Exception) -> None:
    """Failure policy (SPEC §9): backoff counters; auto-disable after N days."""
    now = datetime.now(UTC)
    feed.consecutive_failures += 1
    if feed.first_failure_at is None:
        feed.first_failure_at = now
    feed.last_error = f"{type(exc).__name__}: {exc}"[:1000]
    feed.last_fetched_at = now  # backoff is measured from last attempt

    disabled = False
    if now - feed.first_failure_at >= timedelta(days=settings.feed_disable_after_days):
        feed.is_enabled = False
        disabled = True

    await activity.emit(
        session,
        "ingest",
        "feed_disabled" if disabled else "feed_poll_error",
        {
            "feed": feed.title or feed.url,
            "error": feed.last_error,
            "consecutive_failures": feed.consecutive_failures,
        },
        level="error",
    )
    await session.commit()
