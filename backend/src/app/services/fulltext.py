"""Full-text fetch chain (SPEC §9, invariant 8).

Order — stop at first success:
  1. direct fetch of the source URL (with per-feed cookies if configured)
  2. archive.is (https://archive.is/newest/<url>)
  3. RSS excerpt → content_status=partial + content_warning

Polite crawling: per-domain rate limit, robots.txt respected, archive.is failures
cached for `archive_failure_cache_hours`. All extraction runs via anyio.to_thread.
"""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlparse

import anyio
import httpx
import robots
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
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
# archive.is answers 429 to non-browser agents, so lookups there present as Safari.
ARCHIVE_HOSTS = frozenset({"archive.is", "archive.ph", "archive.today"})
ARCHIVE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
MIN_INTERVAL_S = 2.0  # per-domain rate limit

_robots_cache: dict[str, tuple[robots.RobotsParser, float]] = {}
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


def _fetch_robots_sync(robots_url: str) -> str | None:
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.get(robots_url, headers={"User-Agent": USER_AGENT})
        return resp.text if resp.status_code < 400 else None
    except httpx.HTTPError:
        return None


def _robots_allowed_sync(url: str) -> bool:
    """True unless robots.txt disallows this URL for our agent (or *).

    Uses the `robots` package (robotspy): it matches our UA against its own
    group, else the `*` group. stdlib robotparser instead merges every group
    and is confused by sites that add explicit `Allow: /` for known bots
    (numerama.com), denying everyone else.
    """
    domain = _domain(url)
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{domain}/robots.txt"
    cached = _robots_cache.get(domain)
    if cached and time.monotonic() - cached[1] < 3600:
        rp = cached[0]
    else:
        body = _fetch_robots_sync(robots_url)
        if body is None:
            return True  # unreachable robots.txt → allowed
        rp = robots.RobotsParser.from_string(body)
        _robots_cache[domain] = (rp, time.monotonic())
    return bool(rp.can_fetch(USER_AGENT, url))


async def _fetch_page(url: str, cookies: dict[str, str] | None = None) -> str | None:
    """HTTP GET → HTML, or None on failure. Isolated for testability."""
    await _rate_limit(url)
    allowed = await anyio.to_thread.run_sync(_robots_allowed_sync, url)
    if not allowed:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            agent = ARCHIVE_USER_AGENT if _domain(url) in ARCHIVE_HOSTS else USER_AGENT
            resp = await client.get(url, headers={"User-Agent": agent}, cookies=cookies)
        if resp.status_code >= 400:
            return None
        return resp.text
    except httpx.HTTPError:
        return None


# NULL bytes / C0 control characters (except tab/newline/CR) are not valid XML:
# trafilatura tolerates them, but the readability fallback crashes deep inside
# lxml's cleaner ("All strings must be XML compatible") on pages that carry them
# (seen on newsletter-linked articles). Strip them once, up front.
_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean_html(html: str) -> str:
    return _BAD_XML_CHARS.sub("", html)


def _extract_text(html: str) -> str | None:
    """Blocking extraction (trafilatura → readability fallback)."""
    import trafilatura

    html = _clean_html(html)
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


def _extract_page_meta(html: str) -> tuple[str | None, str | None]:
    """(image, raw date) from page metadata via trafilatura.

    Image: og:image / twitter:image — most article pages still expose an
    og:image even when the feed has no media enclosure (frandroid, …).
    Date: trafilatura's metadata date — note it GUESSES (URL paths, copyright
    years), so callers must not trust it blindly; `_explicit_page_date`
    re-validates against explicit metadata only.
    """
    import trafilatura

    try:
        meta = trafilatura.extract_metadata(html)
    except Exception:
        return None, None
    if meta is None:
        return None, None
    image = str(meta.image) if meta.image else None
    date = str(meta.date) if meta.date else None
    return image, date


def _parse_page_date(raw: str | None) -> datetime | None:
    """Parse a page-metadata date; naive values are assumed UTC. Dates more
    than a day in the future are bogus (clock-skewed CMSs) and rejected."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if dt > datetime.now(UTC) + timedelta(days=1):
        return None
    return dt


# Explicit publication-date metadata — and nothing else. trafilatura's own
# extractor guesses from URL paths (anthropic.com/research/x → 2023-11-03) and
# copyright years (bfl.ai → Jan 1st), which made newsletter story dates random.
# Only a site that declares its publication date in standard metadata is trusted;
# everything else keeps the email's Date: header (in doubt → email date).
_DATE_META_RE = re.compile(
    r"<meta[^>]+(?:property|name|itemprop)\s*=\s*['\"]([^'\"]+)['\"][^>]*"
    r"content\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_DATE_META_RE_REV = re.compile(
    r"<meta[^>]+content\s*=\s*['\"]([^'\"]+)['\"][^>]*"
    r"(?:property|name|itemprop)\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_DATE_KEYS = {
    "article:published_time",
    "og:published_time",
    "datepublished",
    "date",
    "dc.date",
    "dc.date.issued",
    "dcterms.created",
    "sailthru.date",
    "publishdate",
    "pubdate",
    "parsely-pub-date",
    "date.created",
}
_JSONLD_DATE_KEYS = ("datePublished", "dateCreated")
_JSONLD_RE = re.compile(
    r"<script[^>]+type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _jsonld_dates(node: object) -> list[str]:
    """Collect date strings from a JSON-LD node (recursively)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _JSONLD_DATE_KEYS and isinstance(value, str):
                found.append(value)
            elif isinstance(value, (dict, list)):
                found.extend(_jsonld_dates(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_jsonld_dates(item))
    return found


def _strip_archive_chrome(html: str) -> str:
    """Keep only the snapshotted page: drop archive.is's header, share box and toolbar."""
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(_clean_html(html))
    except (ValueError, lxml_html.etree.ParserError):
        return html
    content = doc.xpath('//*[@id="CONTENT"]')
    if content:
        return str(lxml_html.tostring(content[0], encoding="unicode"))
    for el in doc.xpath('//*[@id="HEADER" or @id="DIVSHARE" or @id="SOLID"]'):
        el.drop_tree()
    return str(lxml_html.tostring(doc, encoding="unicode"))


def _explicit_page_date(html: str) -> datetime | None:
    """Publication date from EXPLICIT metadata only (og/article meta, JSON-LD).

    Returns None when nothing explicit is declared — callers then keep the
    email date. Never returns a date derived from URL slugs or copyright text.
    """
    candidates: list[str] = []
    for match in _DATE_META_RE.finditer(html):
        key, value = match.group(1).strip().lower(), match.group(2).strip()
        if key in _DATE_KEYS:
            candidates.append(value)
    for match in _DATE_META_RE_REV.finditer(html):
        value, key = match.group(1).strip(), match.group(2).strip().lower()
        if key in _DATE_KEYS:
            candidates.append(value)
    for block in _JSONLD_RE.findall(html):
        try:
            candidates.extend(_jsonld_dates(json.loads(block)))
        except (ValueError, TypeError):
            continue
    dates = [d for d in (_parse_page_date(c) for c in candidates) if d is not None]
    return min(dates) if dates else None


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
    reason: str | None = None
    date_recovered = False

    # 1. direct
    html = await _fetch_page(article.url, cookies=cookies)
    if html:
        page_image, _page_meta_date = await anyio.to_thread.run_sync(_extract_page_meta, html)
        # Feeds without media enclosures still publish an og:image on the page;
        # recover it here so the story gets a lead image (cluster backfills it).
        if article.image_url is None:
            article.image_url = page_image
        # Newsletter articles start with the email's Date: header (delivery
        # time). The page's real publication date is better — but ONLY when it
        # comes from explicit metadata (og/article meta or JSON-LD). Guessed
        # dates (URL paths, copyright years) would make stories sort randomly,
        # so in any doubt the email date stays. RSS entries keep their
        # feedparser date regardless — it is authoritative.
        if feed.kind == "mail":
            page_date = _explicit_page_date(html)
            if page_date is not None and page_date != article.published_at:
                article.published_at = page_date
                date_recovered = True
        candidate = await anyio.to_thread.run_sync(_extract_text, html)
        if candidate and len(candidate) >= settings.fulltext_min_chars:
            if not _looks_paywalled(candidate):
                text, path = candidate, "direct"
            else:
                reason = "paywall detected"
        elif candidate is not None:
            reason = "extracted text too short"
    else:
        reason = "blocked by robots.txt or site unreachable"

    # 2. archive.is (with per-URL failure cache)
    if text is None:
        failed_at = _archive_failures.get(article.url)
        cache_ok = failed_at is not None and (
            time.time() - failed_at < settings.archive_failure_cache_hours * 3600
        )
        if not cache_ok:
            archived = await _fetch_page(_archive_url(article.url))
            if archived:
                stripped = await anyio.to_thread.run_sync(_strip_archive_chrome, archived)
                candidate = await anyio.to_thread.run_sync(_extract_text, stripped)
                if (
                    candidate
                    and len(candidate) >= settings.fulltext_min_chars
                    and not _looks_paywalled(candidate)
                ):
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
        article.content_warning = reason or (
            "source does not provide full articles; requires credentials"
        )
    else:
        article.content_status = "full"
        article.content_warning = None  # clear any warning left by a previous run

    article.full_text = text
    article.processing_state = "fulltext"
    await activity.emit(
        session,
        "fulltext",
        "fulltext_fetch",
        {
            "article_id": article.id,
            "path": path,
            "chars": len(text or ""),
            "reason": reason,
            "image_recovered": bool(article.image_url),
            "date_recovered": date_recovered,
        },
        level="info" if path != "rss_only" else "warn",
    )


# Bounded concurrency for a batch of full-text fetches. Each fetch runs in its
# OWN short-lived session so concurrent tasks never share an AsyncSession (not
# thread/task-safe) and each article commits independently (writer lock held
# only for the quick UPDATE, never across the network — the codebase rule).
FULLTEXT_CONCURRENCY = 6


async def fetch_full_text_batch(feed_id: int, article_ids: list[int]) -> list[int]:
    """Fetch full text for many articles concurrently; returns ids now ready
    for the LLM queue (state 'fulltext'). Per-article failures are isolated and
    leave the article at 'fetched' for the backlog sweep.

    The per-domain rate limiter (_rate_limit) is shared via module state, so
    concurrency never violates polite crawling — parallel tasks to the SAME
    domain still serialize on the 2s gap.
    """
    if not article_ids:
        return []
    sem = anyio.Semaphore(FULLTEXT_CONCURRENCY)
    ready: list[int] = []

    async def _one(article_id: int) -> None:
        async with sem:
            try:
                async for session in get_session():
                    art = await session.get(Article, article_id)
                    feed = await session.get(Feed, feed_id)
                    if art is None or feed is None:
                        break
                    await fetch_full_text(session, art, feed)
                    await session.commit()
                    ready.append(article_id)
                    break
            except Exception as exc:
                # isolate failures: leave the article 'fetched' for the sweep
                async for session in get_session():
                    await activity.emit(
                        session,
                        "fulltext",
                        "fulltext_fetch",
                        {"article_id": article_id, "path": "error", "reason": str(exc)[:300]},
                        level="error",
                    )
                    await session.commit()
                    break

    async with anyio.create_task_group() as tg:
        for article_id in article_ids:
            tg.start_soon(_one, article_id)
    return ready
