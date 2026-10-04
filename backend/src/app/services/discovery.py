"""Feed discovery service (aligned with the Apple standalone pipeline).

Leads come from three independent sources that are merged and ranked:
- the feed directory's curated topic lists (see `feed_directory`), or its keyword search for
  free text no topic matches;
- publishers mentioned by Google News for the same terms;
- in "smart" mode, publications suggested by the configured LLM.

Every lead is then verified live: the feed must parse, be recent, be in the requested
language, and (when it did not come from a curated list) cover the requested topic.
Feeds the user already follows and duplicates are never returned.
"""

import asyncio
import calendar
import concurrent.futures
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import anyio
import feedparser
import httpx
from langdetect import detect
from langdetect.lang_detect_exception import LangDetectException
from lxml import html as lxml_html
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import activity, feed_directory, llm_client, llmtrace, prompts, usage

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 NewsGator/0.1"
)


async def _search_feedsearch(domain_or_url: str) -> list[str]:
    """Query Feedsearch API for verified feeds on a domain or URL."""
    clean = domain_or_url.strip()
    if clean.startswith("http://") or clean.startswith("https://"):
        clean = urlparse(clean).netloc
    if not clean:
        return []
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=3.5,
        ) as client:
            resp = await client.get(f"https://feedsearch.dev/api/v1/search?url={clean}")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return [
                        f["url"]
                        for f in data
                        if isinstance(f, dict) and isinstance(f.get("url"), str)
                    ]
    except Exception:
        pass
    return []


def _clean_feed_title(raw: str) -> str:
    """Normalize verbose RSS feed titles into clean publication display names."""
    trimmed = raw.strip()
    if " | " in trimmed:
        parts = trimmed.split(" | ")
        if len(parts[-1].strip()) < len(parts[0].strip()) and len(parts[-1].strip()) >= 3:
            return parts[-1].strip()
    if " : " in trimmed:
        prefix = trimmed.split(" : ")[0].strip()
        if 3 <= len(prefix) <= 35:
            return prefix
    return trimmed


def _detect_paywall_markers(text: str) -> bool:
    """Look for standard paywall and subscription markers."""
    pattern = (
        r"\b(abonn[ée]s?|subscribers?|paywall|r[ée]serv[ée] aux abonn[ée]s|"
        r"edition abonn[ée]|premium)\b"
    )
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def _check_html_paywall(html_text: str) -> bool:
    """Check if HTML markup explicitly declares a paywall or subscriber lockout."""
    if re.search(r'["\']isAccessibleForFree["\']\s*:\s*false', html_text, flags=re.IGNORECASE):
        return True
    return bool(
        re.search(
            r'class=["\'][^"\']*\b(paywall|article-paywall|reserve-abonnes|abonne-only)\b',
            html_text,
            flags=re.IGNORECASE,
        )
    )


def _parse_feed_sync(url: str, html_paywalled: bool = False) -> dict[str, Any] | None:
    """Blocking feedparser validation called via anyio.to_thread."""
    try:
        parsed_u = urlparse(url)
        if any(
            x in parsed_u.netloc.lower()
            for x in ("google.", "duckduckgo.", "bing.", "yahoo.", "youtube.")
        ):
            return None

        parsed = feedparser.parse(url)
        feed_meta = getattr(parsed, "feed", {})
        entries = getattr(parsed, "entries", [])
        title = feed_meta.get("title") or ""
        if not title and not entries:
            return None
        site_url = feed_meta.get("link") or ""
        desc = feed_meta.get("subtitle") or feed_meta.get("description") or ""
        sample_titles = [e.get("title", "") for e in entries[:4] if e.get("title")]
        sample_links = [e.get("link", "") for e in entries[:6] if e.get("link")]

        has_paywall_marker = html_paywalled
        content_lengths: list[int] = []

        for e in entries[:5]:
            e_title = e.get("title", "")
            e_desc = e.get("description", "") or e.get("summary", "")
            if _detect_paywall_markers(e_title) or _detect_paywall_markers(e_desc):
                has_paywall_marker = True

            content_list = e.get("content", [])
            body_len = 0
            if content_list and isinstance(content_list, list):
                body_len = max(len(c.get("value", "")) for c in content_list if isinstance(c, dict))
            if not body_len and e_desc:
                body_len = len(e_desc)
            if body_len > 0:
                content_lengths.append(body_len)

        if not has_paywall_marker and sample_links:
            valid_links = [
                link
                for link in sample_links[:5]
                if isinstance(link, str) and link.startswith("http")
            ]
            if valid_links:

                def _check_link(link_url: str) -> bool:
                    try:
                        with httpx.Client(
                            headers={"User-Agent": USER_AGENT},
                            follow_redirects=True,
                            timeout=3.0,
                        ) as client:
                            resp = client.get(link_url)
                            return resp.status_code == 200 and _check_html_paywall(resp.text)
                    except Exception:
                        return False

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(valid_links), 5)
                ) as executor:
                    futures = [executor.submit(_check_link, link) for link in valid_links]
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            if future.result():
                                has_paywall_marker = True
                                break
                        except Exception:
                            pass

        has_full_text = bool(
            content_lengths and (sum(content_lengths) / len(content_lengths)) >= 800
        )
        access_level = (
            "paywalled"
            if has_paywall_marker
            else ("free_full" if has_full_text else "free_excerpt")
        )

        sample_articles = [
            {
                "title": str(e.get("title", "")).strip(),
                "url": str(e.get("link", "")).strip(),
                "published_at": str(e.get("published", "") or e.get("updated", "")).strip(),
            }
            for e in entries[:5]
            if e.get("title")
        ]

        stamps = [
            calendar.timegm(t)
            for e in entries
            if (t := e.get("published_parsed") or e.get("updated_parsed"))
        ]

        return {
            "url": url,
            "newest_ts": float(max(stamps)) if stamps else None,
            "title": str(title).strip() or urlparse(url).netloc,
            "site_url": str(site_url).strip() or f"https://{urlparse(url).netloc}",
            "description": str(desc).strip(),
            "sample_titles": sample_titles,
            "sample_articles": sample_articles,
            "access_level": access_level,
        }
    except Exception:
        return None


async def _probe_url(url: str) -> dict[str, Any] | None:
    """Probe a candidate URL. If direct feedparser parses it, returns metadata.
    If it's an HTML page, extracts <link rel="alternate"> feed candidates and tests them."""
    if url.startswith("http://"):
        url = "https://" + url[7:]

    res = await anyio.to_thread.run_sync(_parse_feed_sync, url, False)
    if res is not None:
        return res

    parsed_u = urlparse(url)
    if not parsed_u.netloc or any(
        x in parsed_u.netloc.lower()
        for x in ("google.", "duckduckgo.", "bing.", "yahoo.", "youtube.")
    ):
        return None

    path_lower = parsed_u.path.lower()
    is_likely_feed = any(path_lower.endswith(ext) for ext in (".xml", ".atom", ".rss")) or any(
        x in path_lower for x in ("/feed", "/rss")
    )
    if is_likely_feed:
        # Do not replace specific topical feed path failures with generic root domain feeds
        return None

    html_paywalled = False
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=6.0,
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and "html" in resp.headers.get("content-type", "").lower():
                if re.search(
                    r'["\']isAccessibleForFree["\']\s*:\s*false', resp.text, flags=re.IGNORECASE
                ):
                    html_paywalled = True

                tree = lxml_html.fromstring(resp.text)
                feed_links = tree.xpath(
                    '//link[contains(@rel, "alternate") and ('
                    '@type="application/rss+xml" or '
                    '@type="application/atom+xml" or '
                    '@type="application/feed+json")]/@href'
                )
                for fl in feed_links:
                    candidate = urljoin(str(resp.url), fl)
                    cand_res = await anyio.to_thread.run_sync(
                        _parse_feed_sync, candidate, html_paywalled
                    )
                    if cand_res is not None:
                        return cand_res
    except Exception:
        pass

    base = f"{parsed_u.scheme}://{parsed_u.netloc}"
    for path in ("/feed", "/feed/", "/rss", "/feed.xml", "/rss.xml", "/index.xml", "/atom.xml"):
        cand_url = urljoin(base, path)
        cand_res = await anyio.to_thread.run_sync(_parse_feed_sync, cand_url, html_paywalled)
        if cand_res is not None:
            return cand_res

    if parsed_u.netloc:
        fs_urls = await _search_feedsearch(parsed_u.netloc)
        for cand_url in fs_urls:
            cand_res = await anyio.to_thread.run_sync(_parse_feed_sync, cand_url, html_paywalled)
            if cand_res is not None:
                return cand_res

    return None


# --- discovery pipeline ---------------------------------------------------------------

MAX_LEADS = 60
TARGET_RESULTS = 30
PROBE_CONCURRENCY = 6
MAX_NEWS_HOSTS = 25
STALE_DAYS = 180
FRESH_DAYS = 30
BLOCKED_HOSTS = ("google.", "youtube.", "facebook.", "twitter.", "duckduckgo.", "bing.", "yahoo.")
NEWS_URL = "https://news.google.com/rss/search"


@dataclass
class _Lead:
    site_url: str
    feed_url: str | None = None
    title: str = ""
    description: str = ""
    subscribers: int = 0
    language: str | None = None
    icon_url: str | None = None
    curated: bool = False
    mentions: int = 0
    terms_matched: int = 0
    sources: set[str] = field(default_factory=set)
    pre_score: float = 0.0


def feed_key(url: str) -> str:
    """Identity of a feed: scheme, `www.`, case, trailing slash and fragment are ignored."""
    p = urlparse(url.strip())
    key = f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"
    return f"{key}?{p.query}" if p.query else key


def site_key(url_or_host: str) -> str:
    host = urlparse(url_or_host).netloc if "://" in url_or_host else url_or_host
    return host.lower().removeprefix("www.")


def _blocked(host: str) -> bool:
    h = host.lower()
    return (
        not h
        or any(b in h for b in BLOCKED_HOSTS)
        or h in ("x.com", "www.x.com")
        or h.endswith(".x.com")
        or "reddit." in h
    )


def _https(url: str) -> str:
    return "https://" + url[7:] if url.startswith("http://") else url


def _directory_lead(item: dict[str, Any], curated: bool) -> _Lead | None:
    feed_id = str(item.get("feed_id") or "")
    url = _https(feed_id[5:] if feed_id.startswith("feed/") else feed_id)
    host = urlparse(url).netloc
    if not url.startswith("https://") or _blocked(host):
        return None
    site = _https(str(item.get("website") or f"https://{host}"))
    lang = item.get("language")
    return _Lead(
        site_url=site,
        feed_url=url,
        title=str(item.get("title") or ""),
        description=str(item.get("description") or ""),
        subscribers=int(item.get("subscribers") or 0),
        language=str(lang).lower()[:2] if lang else None,
        icon_url=str(item["icon_url"]) if item.get("icon_url") else None,
        curated=curated,
        sources={"directory"},
    )


async def _directory_leads(
    themes: list[str], text: str, language: str | None
) -> tuple[list[_Lead], int]:
    """Leads from curated topics (or the keyword search fallback) and the number of terms."""
    batches: list[tuple[list[dict[str, Any]], bool]] = []
    if themes:
        for theme in themes:
            topic = feed_directory.CATEGORY_TOPICS.get(theme)
            if topic:
                batches.append((await feed_directory.topic_feeds(topic, language), True))
            else:
                batches.append((await feed_directory.search_feeds(theme, language, 100), False))
    else:
        for topic in feed_directory.focus_topics(text):
            batches.append((await feed_directory.topic_feeds(topic, language), True))
        if not any(items for items, _ in batches) and text:
            batches = [(await feed_directory.search_feeds(text, language, 100), False)]
    by_key: dict[str, _Lead] = {}
    for items, curated in batches:
        for item in items:
            lead = _directory_lead(item, curated)
            if lead is None or lead.feed_url is None:
                continue
            existing = by_key.get(feed_key(lead.feed_url))
            if existing is None:
                lead.terms_matched = 1
                by_key[feed_key(lead.feed_url)] = lead
            else:
                existing.terms_matched += 1
                existing.curated = existing.curated or curated
    return list(by_key.values()), max(len(batches), 1)


async def _translate_term(
    session: AsyncSession, term: str, language: str | None
) -> str:
    """Translate a search term to the edition language; falls back to the original."""
    if not language or language == "en" or not llm_client.is_configured():
        return term
    system, user = prompts.discovery_translate_term(term, language)
    try:
        with llmtrace.context("discovery_translate", label=f"Translate '{term}'"):
            parsed, latency = await llm_client.chat_json(system, user)
        usage.record(
            session,
            kind="discovery_translate",
            endpoint="chat",
            model=settings.llm_model,
            latency_ms=latency,
            prompt_chars=len(system) + len(user),
            completion_chars=len(str(parsed)),
        )
        value = str(parsed.get("term") or "").strip() if isinstance(parsed, dict) else ""
        return value or term
    except Exception:
        return term


def _parse_news_sources(text: str) -> dict[str, tuple[int, str]]:
    """host -> (mention count, publisher name) from a news RSS result list."""
    counts: dict[str, tuple[int, str]] = {}
    for entry in feedparser.parse(text).entries:
        source = entry.get("source")
        href = source.get("href") if isinstance(source, dict) else None
        if not isinstance(href, str):
            continue
        host = site_key(href)
        if _blocked(host):
            continue
        n, name = counts.get(host, (0, ""))
        counts[host] = (n + 1, name or str(source.get("title") or ""))
    return counts


async def _news_mentions(term: str, language: str, country: str) -> dict[str, tuple[int, str]]:
    params = {"q": term, "hl": language, "gl": country, "ceid": f"{country}:{language}"}
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=8.0
        ) as client:
            resp = await client.get(NEWS_URL, params=params)
        if resp.status_code != 200:
            return {}
        return await anyio.to_thread.run_sync(_parse_news_sources, resp.text)
    except Exception:
        return {}


def _merge_news(leads: list[_Lead], mentions: dict[str, tuple[int, str]]) -> list[_Lead]:
    """Add news mentions to matching directory leads; unmatched publishers become new leads."""
    ranked = sorted(mentions.items(), key=lambda kv: kv[1][0], reverse=True)[:MAX_NEWS_HOSTS]
    for host, (count, name) in ranked:
        matched = False
        for lead in leads:
            if site_key(lead.site_url) == host or (
                lead.feed_url and site_key(lead.feed_url) == host
            ):
                lead.mentions += count
                lead.sources.add("news")
                matched = True
        if not matched:
            leads.append(
                _Lead(site_url=f"https://{host}", title=name, mentions=count, sources={"news"})
            )
    return leads


async def _suggested_leads(
    session: AsyncSession, request: str, lang_code: str | None, country: str | None
) -> list[_Lead]:
    system, user = prompts.discovery_suggest_feeds(request, lang_code, country)
    try:
        with llmtrace.context("discovery_suggest", label=f"Feed suggestions for {request[:60]}"):
            parsed, latency = await llm_client.chat_json(system, user)
    except Exception:
        return []
    usage.record(
        session,
        kind="discovery_suggest",
        endpoint="chat",
        model=settings.llm_model,
        latency_ms=latency,
        prompt_chars=len(system) + len(user),
        completion_chars=len(str(parsed)),
    )
    items = parsed.get("suggestions") if isinstance(parsed, dict) else None
    leads: list[_Lead] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        site = _https(str(item.get("website") or "").strip())
        feed = _https(str(item.get("feed_url") or "").strip())
        if not site.startswith("https://") or _blocked(urlparse(site).netloc):
            continue
        leads.append(
            _Lead(
                site_url=site,
                feed_url=feed if feed.startswith("https://") else None,
                title=str(item.get("name") or ""),
                sources={"ai"},
            )
        )
    return leads


def _pre_score(lead: _Lead, max_mentions: int, term_count: int) -> float:
    score = 1.0 if {"directory", "news"} <= lead.sources else 0.0
    score += 1.5 * lead.mentions / max_mentions
    score += 1.0 * lead.terms_matched / term_count
    score += 3.0 * min(1.0, math.log10(lead.subscribers + 1) / 6)
    return score


async def _probe_lead(lead: _Lead) -> dict[str, Any] | None:
    urls = [u for u in (lead.feed_url, lead.site_url) if u]
    if len(urls) == 2 and feed_key(urls[0]) == feed_key(urls[1]):
        urls = urls[:1]
    for url in urls:
        res = await _probe_url(url)
        if res is not None:
            return res
    return None


def _language_matches(lead: _Lead, res: dict[str, Any], language: str | None) -> bool:
    if not language:
        return True
    declared = lead.language
    if declared is None:
        text = " ".join(res.get("sample_titles") or [])
        if len(text) < 20:
            return True
        try:
            declared = detect(text).lower()[:2]
        except LangDetectException:
            return True
    return declared == language


async def _covers_topic(
    session: AsyncSession, terms: list[str], res: dict[str, Any], lead: _Lead
) -> bool:
    if lead.curated or not terms or not llm_client.is_configured():
        return True
    system, user = prompts.discovery_topic_check(
        terms,
        str(res.get("title") or lead.title),
        str(res.get("description") or lead.description),
        list(res.get("sample_titles") or []),
    )
    try:
        with llmtrace.context("discovery_topic_check", label=str(res.get("title"))[:80]):
            parsed, latency = await llm_client.chat_json(system, user)
    except Exception:
        return True
    usage.record(
        session,
        kind="discovery_topic_check",
        endpoint="chat",
        model=settings.llm_model,
        latency_ms=latency,
        prompt_chars=len(system) + len(user),
        completion_chars=len(str(parsed)),
    )
    return not (isinstance(parsed, dict) and parsed.get("covers") is False)


def _country_name(language: str | None, country: str | None) -> str | None:
    if not country:
        return None
    try:
        import babel

        return str(babel.Locale(language or "en").territories.get(country)) or None
    except Exception:
        return None


async def discover_feeds(
    session: AsyncSession,
    *,
    location: str = "",
    themes: list[str] | None = None,
    query: str = "",
    mode: str = "smart",
    locale: str | None = None,
    excluded_urls: list[str] | None = None,
    lang_code: str | None = None,
) -> list[dict[str, Any]]:
    """Find, verify and rank feeds for categories or a free-text request.

    `themes` (category names) and free text are exclusive: categories win when both are sent.
    `excluded_urls` are never returned (feeds the user already follows, plus any the client
    adds). Language and country come from `locale` (e.g. `fr_FR`).
    """
    started = time.monotonic()
    themes = [t.strip() for t in (themes or []) if t.strip()]
    text = "" if themes else (query.strip() or location.strip())
    lang_part, _, region = (locale or "").replace("-", "_").partition("_")
    language = lang_part.lower() or None
    country = region.upper() or None
    terms = themes or ([text] if text else [])
    use_smart = mode == "smart" and llm_client.is_configured() and bool(terms)

    await activity.emit(
        session,
        component="discovery",
        action="discover_start",
        detail={
            "mode": "smart" if use_smart else "catalog",
            "themes": themes,
            "query": text,
            "locale": locale,
        },
    )

    excluded = {feed_key(u) for u in (excluded_urls or [])}
    term_count = 1
    leads: list[_Lead]
    if use_smart:
        request = ", ".join(terms)
        leads = await _suggested_leads(
            session, request, language or lang_code, _country_name(language, country)
        )
    else:
        leads, term_count = await _directory_leads(themes, text, language)
        if language and country:
            news_terms = terms or ["news"]
            translated = await asyncio.gather(
                *(_translate_term(session, t, language) for t in news_terms)
            )
            batches = await asyncio.gather(
                *(_news_mentions(t, language, country) for t in translated)
            )
            merged: dict[str, tuple[int, str]] = {}
            for batch in batches:
                for host, (count, name) in batch.items():
                    old = merged.get(host, (0, name))
                    merged[host] = (old[0] + count, old[1] or name)
            leads = _merge_news(leads, merged)

    max_mentions = max([lead.mentions for lead in leads] + [1])
    for lead in leads:
        lead.pre_score = _pre_score(lead, max_mentions, term_count)
    leads = sorted(leads, key=lambda ld: ld.pre_score, reverse=True)
    leads = [
        ld for ld in leads if not (ld.feed_url and feed_key(ld.feed_url) in excluded)
    ][:MAX_LEADS]

    found: list[tuple[float, dict[str, Any]]] = []
    seen = set(excluded)
    gate = asyncio.Semaphore(PROBE_CONCURRENCY)
    now = time.time()

    async def check(lead: _Lead) -> None:
        async with gate:
            if len(found) >= TARGET_RESULTS:
                return
            res = await _probe_lead(lead)
        if res is None:
            return
        key = feed_key(str(res["url"]))
        if key in seen:
            return
        newest = res.get("newest_ts")
        if newest is not None and now - newest > STALE_DAYS * 86400:
            return
        if not _language_matches(lead, res, language):
            return
        if not await _covers_topic(session, terms, res, lead):
            return
        if key in seen:
            return
        seen.add(key)
        score = lead.pre_score
        if language:
            score += 1.0
        if newest is not None and now - newest <= FRESH_DAYS * 86400:
            score += 1.0
        labels = {"directory": "Curated list", "news": "In the news", "ai": "Suggested by AI"}
        reason = ", ".join(labels[s] for s in sorted(lead.sources))
        if lead.subscribers:
            reason += f" · {lead.subscribers:,} subscribers"
        found.append(
            (
                score,
                {
                    "title": _clean_feed_title(str(res["title"])) or lead.title,
                    "url": res["url"],
                    "site_url": res.get("site_url") or lead.site_url,
                    "description": res.get("description") or lead.description,
                    "match_reason": reason,
                    "icon_url": lead.icon_url,
                    "access_level": res.get("access_level", "free_excerpt"),
                    "geographic_scope": "national",
                    "sample_articles": res.get("sample_articles", []),
                    "sources": sorted(lead.sources),
                },
            )
        )

    # Verification is bounded: a slow LLM or site must not turn into a proxy 504.
    remaining = started + settings.discovery_budget_s - time.monotonic()
    tasks = [asyncio.create_task(check(ld)) for ld in leads]
    _, pending = await asyncio.wait(tasks, timeout=max(1.0, remaining))
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    final = [f for _, f in sorted(found, key=lambda x: x[0], reverse=True)]

    await activity.emit(
        session,
        component="discovery",
        action="discover_done",
        detail={"count": len(final)},
    )
    await session.commit()
    return final


