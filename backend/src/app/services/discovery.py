"""LLM-driven & Catalog-driven feed discovery service.

Supports two distinct discovery modes:
1. Catalog Search (Deterministic):
   Direct directory catalog lookup using #topic tags or city/region keywords with locale,
   sorted strictly by subscribers descending. Zero AI required.
2. Smart Search (AI):
   Multi-turn LLM query formulation and synthesis to discover authoritative, local,
   and independent media outlets and publisher domains.
"""

import asyncio
import concurrent.futures
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import anyio
import feedparser
import httpx
from lxml import html as lxml_html
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import activity, llm_client, llmtrace, prompts, usage

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 NewsGator/0.1"
)


def _resolve_country_code(loc: str) -> str | None:
    """Resolve location to standard ISO country code dynamically using babel."""
    if not loc:
        return None
    loc_clean = loc.strip().lower()
    try:
        import babel

        for lang in ("en", "fr", "de", "es", "it"):
            locale = babel.Locale(lang)
            for code, name in locale.territories.items():
                if len(code) == 2 and name.lower() == loc_clean:
                    return str(code)
    except Exception:
        pass
    return None


async def _search_catalog(
    query: str,
    locale: str | None = None,
    count: int = 20,
) -> list[dict[str, Any]]:
    """Query directory catalog API for candidate feeds."""
    clean = query.strip()
    if not clean:
        return []
    params: dict[str, Any] = {"query": clean, "count": count}
    if locale:
        params["locale"] = locale
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=5.0,
        ) as client:
            resp = await client.get("https://cloud.feedly.com/v3/search/feeds", params=params)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results")
                if isinstance(results, list):
                    return [r for r in results if isinstance(r, dict)]
    except Exception:
        pass
    return []


def _extract_catalog_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    """Extract and validate feed metadata from a directory catalog item."""
    feed_id = str(item.get("feedId") or item.get("id") or "").strip()
    url = feed_id[5:] if feed_id.startswith("feed/") else feed_id
    if not url.startswith(("http://", "https://")):
        return None
    if url.startswith("http://"):
        url = "https://" + url[7:]
    parsed = urlparse(url)
    if not parsed.netloc or any(
        b in parsed.netloc.lower()
        for b in ("google.", "youtube.", "duckduckgo.", "bing.", "yahoo.")
    ):
        return None

    site_url = str(item.get("website") or f"https://{parsed.netloc}").strip()
    if site_url.startswith("http://"):
        site_url = "https://" + site_url[7:]
    subscribers = int(item.get("subscribers") or 0)
    title = str(item.get("title") or parsed.netloc).strip()
    desc = str(item.get("description") or "").strip()
    icon = item.get("iconUrl") or item.get("visualUrl")

    return {
        "url": url,
        "title": title,
        "site_url": site_url,
        "description": desc,
        "subscribers": subscribers,
        "icon_url": str(icon).strip() if icon else None,
    }


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


def _catalog_tag(theme: str) -> str:
    """Normalize a theme into a directory catalog tag."""
    t = theme.lower().strip()
    if "artif" in t or t == "ai":
        return "artificialintelligence"
    if "cyber" in t or "secur" in t:
        return "cybersecurity"
    if "tech" in t:
        return "tech"
    if "game" in t or "gaming" in t:
        return "gaming"
    if "sci" in t:
        return "science"
    if "health" in t:
        return "health"
    if "politic" in t:
        return "politics"
    if "finan" in t:
        return "finance"
    if "busin" in t:
        return "business"
    if "sport" in t:
        return "sports"
    if "env" in t or "climat" in t:
        return "environment"
    if "cult" in t:
        return "culture"
    if "news" in t:
        return "news"
    clean = "".join(c for c in t if c.isalnum())
    return clean or "news"


async def _search_directory_feeds(
    location: str = "",
    themes: list[str] | None = None,
    query: str = "",
    locale: str | None = None,
) -> list[str]:
    """Query open feed directory / Feedsearch for candidate feeds matching location or themes."""
    results: list[str] = []
    q_clean = query.strip()
    loc_clean = location.strip()

    # 1 search = 1 API call! Country selection is ONLY passed as locale, never as query.
    if q_clean:
        catalog_query = q_clean
    elif themes:
        tags = " ".join(f"#{_catalog_tag(t)}" for t in themes if t.strip())
        catalog_query = (
            f"{tags} {loc_clean}".strip()
            if loc_clean and loc_clean.lower() not in ("global", "worldwide")
            else tags
        )
    elif loc_clean and loc_clean.lower() not in ("global", "worldwide"):
        catalog_query = loc_clean
    else:
        catalog_query = "#news"

    cat_items = await _search_catalog(catalog_query, locale=locale, count=20)
    for it in cat_items:
        cand = _extract_catalog_candidate(it)
        if cand and cand["url"] not in results:
            results.append(cand["url"])

    for t in (q_clean, loc_clean):
        if "." in t and not t.startswith("http") and " " not in t and not t.startswith("#"):
            fs_urls = await _search_feedsearch(t)
            for u in fs_urls:
                if u not in results:
                    results.append(u)
    return results


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

        return {
            "url": url,
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


async def _translate_topic_tag(theme: str, locale: str | None, allow_llm: bool = False) -> str:
    """Translate theme keyword to localized tag using LLM if requested/available, else clean tag."""
    t_clean = theme.strip().lower().replace(" ", "").replace("&", "")
    if not allow_llm or not locale:
        return t_clean
    lang = locale.split("_")[0].lower() if "_" in locale else locale.lower()
    if lang == "en":
        return t_clean
    if llm_client.is_configured():
        try:
            sys_prompt = (
                "Translate this single news category keyword to a single-word lowercase "
                "topic tag in the requested language without spaces, punctuation, or accents."
            )
            user_prompt = (
                f"Category: {theme}\nTarget language ISO code: {lang}\n"
                'Output JSON: {"tag": "word"}'
            )
            parsed, _ = await llm_client.chat_json(sys_prompt, user_prompt)
            if isinstance(parsed, dict) and parsed.get("tag"):
                tag = str(parsed["tag"]).strip().lower().replace(" ", "").replace("#", "")
                if tag:
                    return tag
        except Exception:
            pass
    return t_clean


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
    """Execute dual-mode feed discovery (Catalog Search or Smart Search) with live verification."""
    themes = themes or []
    loc_clean = location.strip()
    q_clean = query.strip()
    excluded_set = set(excluded_urls or [])
    excluded_domains = {urlparse(u).netloc.lower() for u in excluded_set if urlparse(u).netloc}

    use_smart = (mode == "smart") and llm_client.is_configured()

    await activity.emit(
        session,
        component="discovery",
        action="discover_start",
        detail={
            "mode": "smart" if use_smart else "catalog",
            "location": loc_clean,
            "themes": themes,
            "query": q_clean,
            "locale": locale,
        },
    )

    is_global = not loc_clean or loc_clean.lower() in ("global", "worldwide")
    scope_level = "global" if is_global else "region"
    country_code = _resolve_country_code(loc_clean)

    loc_words = [w for w in re.findall(r"\w+", loc_clean.lower()) if len(w) >= 3]
    theme_words = [w for t in themes for w in re.findall(r"\w+", t.lower()) if len(w) >= 3]
    q_words = [w for w in re.findall(r"\w+", q_clean.lower()) if len(w) >= 2]

    candidates_to_probe: list[dict[str, Any]] = []
    seen_urls: set[str] = set(excluded_set)
    seen_domains: set[str] = set(excluded_domains)

    # Mode 1: Catalog Search (Deterministic, Directory-based)
    if not use_smart:
        catalog_tasks: list[asyncio.Task[list[dict[str, Any]]]] = []

        if q_clean:
            catalog_tasks.append(
                asyncio.create_task(_search_catalog(q_clean, locale=locale, count=15))
            )
        if loc_clean and loc_clean.lower() not in ("global", "worldwide"):
            catalog_tasks.append(
                asyncio.create_task(_search_catalog(loc_clean, locale=locale, count=15))
            )

        for t in themes:
            tag = await _translate_topic_tag(t, locale)
            catalog_tasks.append(
                asyncio.create_task(_search_catalog(f"#{tag}", locale=locale, count=15))
            )

        if not catalog_tasks:
            catalog_tasks.append(
                asyncio.create_task(_search_catalog("news", locale=locale, count=15))
            )

        catalog_results = await asyncio.gather(*catalog_tasks, return_exceptions=True)

        for res in catalog_results:
            if isinstance(res, list):
                for item in res:
                    cand = _extract_catalog_candidate(item)
                    if not cand:
                        continue
                    u = cand["url"]
                    dom = urlparse(u).netloc.lower()
                    if u not in seen_urls and dom not in seen_domains:
                        seen_urls.add(u)
                        seen_domains.add(dom)
                        candidates_to_probe.append(cand)

        if len(candidates_to_probe) < 10:
            try:
                dir_urls = await _search_directory_feeds(
                    location=loc_clean, themes=themes, query=q_clean, locale=locale
                )
            except TypeError:
                dir_urls = await _search_directory_feeds(
                    location=loc_clean, themes=themes, query=q_clean
                )
            for u in dir_urls:
                dom = urlparse(u).netloc.lower()
                if u not in seen_urls and dom not in seen_domains:
                    seen_urls.add(u)
                    seen_domains.add(dom)
                    candidates_to_probe.append(
                        {
                            "url": u,
                            "title": dom,
                            "site_url": f"https://{dom}",
                            "description": "",
                            "subscribers": 0,
                            "icon_url": None,
                        }
                    )

        candidates_to_probe.sort(key=lambda c: int(c.get("subscribers") or 0), reverse=True)

    # Mode 2: Smart Search (LLM-driven)
    else:
        candidate_urls: list[str] = []
        suggested_domains: list[str] = []

        llmtrace.context("discover_queries", label=f"Discovery queries for {themes or 'all'}")
        sys_prompt, usr_prompt = prompts.discovery_queries(
            loc_clean, themes, q_clean, lang_code=lang_code
        )

        try:
            parsed_q, latency = await llm_client.chat_json(sys_prompt, usr_prompt)
            usage.record(
                session,
                kind="discovery_queries",
                endpoint="chat",
                model=settings.llm_model,
                latency_ms=latency,
                prompt_chars=len(sys_prompt) + len(usr_prompt),
                completion_chars=len(str(parsed_q)),
            )
            if isinstance(parsed_q, dict):
                if parsed_q.get("scope_level") in (
                    "city",
                    "region",
                    "country",
                    "continent",
                    "global",
                ):
                    scope_level = parsed_q["scope_level"]
                for d in parsed_q.get("suggested_domains", []):
                    if isinstance(d, str) and d.strip():
                        suggested_domains.append(d.strip())
                for u in parsed_q.get("candidate_feed_urls", []):
                    if isinstance(u, str) and u.strip().startswith("http"):
                        candidate_urls.append(u.strip())
        except Exception:
            pass

        for u in candidate_urls:
            dom = urlparse(u).netloc.lower()
            if u not in seen_urls and dom not in seen_domains:
                seen_urls.add(u)
                seen_domains.add(dom)
                candidates_to_probe.append(
                    {
                        "url": u,
                        "title": dom,
                        "site_url": f"https://{dom}",
                        "description": "",
                        "subscribers": 0,
                        "icon_url": None,
                    }
                )

        for d in suggested_domains:
            target_url = d if d.startswith("http") else f"https://{d}"
            dom = urlparse(target_url).netloc.lower()
            if target_url not in seen_urls and dom not in seen_domains:
                seen_urls.add(target_url)
                seen_domains.add(dom)
                candidates_to_probe.append(
                    {
                        "url": target_url,
                        "title": dom,
                        "site_url": f"https://{dom}",
                        "description": "",
                        "subscribers": 0,
                        "icon_url": None,
                    }
                )

        try:
            dir_urls = await _search_directory_feeds(
                location=loc_clean, themes=themes, query=q_clean, locale=locale
            )
        except TypeError:
            dir_urls = await _search_directory_feeds(
                location=loc_clean, themes=themes, query=q_clean
            )
        for u in dir_urls:
            dom = urlparse(u).netloc.lower()
            if u not in seen_urls and dom not in seen_domains:
                seen_urls.add(u)
                seen_domains.add(dom)
                candidates_to_probe.append(
                    {
                        "url": u,
                        "title": dom,
                        "site_url": f"https://{dom}",
                        "description": "",
                        "subscribers": 0,
                        "icon_url": None,
                    }
                )

    def _target_priority(c: dict[str, Any]) -> int:
        u_lower = c["url"].lower()
        score = int(c.get("subscribers") or 0) // 100
        if country_code:
            cc = country_code.lower()
            if u_lower.endswith(f".{cc}") or f".{cc}/" in u_lower:
                score += 80
        for w in q_words:
            if w in u_lower:
                score += 50
        for w in theme_words:
            if w in u_lower:
                score += 40
        for w in loc_words:
            if w in u_lower:
                score += 30
        return score

    if use_smart:
        candidates_to_probe.sort(key=_target_priority, reverse=True)

    # Live Verification Loop
    sem = asyncio.Semaphore(6)
    validated_feeds: list[dict[str, Any]] = []
    probed_feed_urls: set[str] = set(excluded_set)
    probed_feed_hosts: set[str] = set(excluded_domains)

    async def _safe_probe(cand: dict[str, Any]) -> None:
        async with sem:
            try:
                res = await _probe_url(cand["url"])
                if res and res["url"] not in probed_feed_urls:
                    res_domain = urlparse(res["url"]).netloc.lower()
                    if res_domain not in probed_feed_hosts:
                        probed_feed_urls.add(res["url"])
                        probed_feed_hosts.add(res_domain)
                        if cand.get("subscribers"):
                            res["subscribers"] = cand["subscribers"]
                        if cand.get("icon_url") and not res.get("icon_url"):
                            res["icon_url"] = cand["icon_url"]
                        validated_feeds.append(res)
            except Exception:
                pass

    probe_tasks = [_safe_probe(c) for c in candidates_to_probe[:30]]
    await asyncio.gather(*probe_tasks, return_exceptions=True)

    def _loc_relevance(item: dict[str, Any]) -> int:
        if is_global or not loc_words:
            return 0
        t = (item.get("title") or "").lower()
        d = (item.get("description") or "").lower()
        u = (item.get("url") or "").lower()
        samples = " ".join(item.get("sample_titles") or []).lower()
        sc = 0
        for w in loc_words:
            if w in t:
                sc += 40
            if w in u:
                sc += 35
            if w in samples:
                sc += 25
            if w in d:
                sc += 15
        return sc

    if not use_smart:
        validated_feeds.sort(key=lambda f: int(f.get("subscribers") or 0), reverse=True)
    else:

        def _relevance_score(feed: dict[str, Any]) -> int:
            t = (feed.get("title") or "").lower()
            d = (feed.get("description") or "").lower()
            u = (feed.get("url") or "").lower()
            samples = " ".join(feed.get("sample_titles") or []).lower()
            score = 0
            if q_clean:
                q_lower = q_clean.lower()
                if q_lower in t or q_lower in u:
                    score += 150
                for w in q_words:
                    if w in t:
                        score += 60
                    if w in u:
                        score += 50
                    if w in samples:
                        score += 30
                    if w in d:
                        score += 20
            for w in theme_words:
                if w in t:
                    score += 50
                if w in u:
                    score += 40
                if w in samples:
                    score += 30
                if w in d:
                    score += 20
            if not is_global:
                if country_code:
                    cc = country_code.lower()
                    if u.endswith(f".{cc}") or f".{cc}/" in u:
                        score += 80
                for w in loc_words:
                    if w in t:
                        score += 40
                    if w in u:
                        score += 35
                    if w in samples:
                        score += 25
                    if w in d:
                        score += 15
            return score

        validated_feeds.sort(key=_relevance_score, reverse=True)

    validated_top = validated_feeds[:10]
    final_feeds: list[dict[str, Any]] = []

    if use_smart and validated_top:
        llmtrace.context(
            "discover_synthesis", label=f"Discovery synthesis for {len(validated_top)} feeds"
        )
        s_sys, s_usr = prompts.discovery_synthesis(
            validated_top, loc_clean, scope_level, themes, q_clean, lang_code=lang_code
        )
        try:
            parsed_s, s_latency = await llm_client.chat_json(s_sys, s_usr)
            usage.record(
                session,
                kind="discovery_synthesis",
                endpoint="chat",
                model=settings.llm_model,
                latency_ms=s_latency,
                prompt_chars=len(s_sys) + len(s_usr),
                completion_chars=len(str(parsed_s)),
            )
            val_by_url = {item["url"]: item for item in validated_top}
            if (
                isinstance(parsed_s, dict)
                and "feeds" in parsed_s
                and isinstance(parsed_s["feeds"], list)
            ):
                for f in parsed_s["feeds"]:
                    if isinstance(f, dict) and f.get("url") in val_by_url:
                        base = val_by_url[f["url"]]
                        acc = f.get("access_level") or base.get("access_level", "free_excerpt")
                        if acc not in ("free_full", "free_excerpt", "paywalled"):
                            acc = base.get("access_level", "free_excerpt")

                        geo = f.get("geographic_scope") or (
                            "local" if _loc_relevance(base) >= 20 else "national"
                        )
                        if geo not in ("local", "regional", "national", "global"):
                            geo = "local" if _loc_relevance(base) >= 20 else "national"

                        final_feeds.append(
                            {
                                "title": _clean_feed_title(f.get("title") or base["title"]),
                                "url": f["url"],
                                "site_url": base.get("site_url"),
                                "description": f.get("description")
                                or base.get("description")
                                or "",
                                "match_reason": f.get("match_reason")
                                or f"Matches {', '.join(themes) or 'news'}",
                                "access_level": acc,
                                "geographic_scope": geo,
                                "sample_articles": base.get("sample_articles") or [],
                                "icon_url": base.get("icon_url")
                                or (
                                    f"{base.get('site_url', '').rstrip('/')}/favicon.ico"
                                    if base.get("site_url")
                                    else None
                                ),
                            }
                        )
        except Exception:
            pass

    if not final_feeds:
        for item in validated_top:
            score = _loc_relevance(item)
            if score >= 20:
                reason = f"Local publication covering {loc_clean}"
                geo_scope = "local"
            elif score > 0 or scope_level == "country":
                reason = f"National publication covering {loc_clean}"
                geo_scope = "national"
            elif not is_global:
                reason = f"Publication for {loc_clean}"
                geo_scope = "national"
            elif themes:
                reason = f"Coverage matching {', '.join(themes)}"
                geo_scope = "global"
            elif item.get("subscribers"):
                reason = f"Popular publication ({item['subscribers']:,} subscribers)"
                geo_scope = "global"
            else:
                reason = "Recommended news publication"
                geo_scope = "global"

            final_feeds.append(
                {
                    "title": _clean_feed_title(item["title"]),
                    "url": item["url"],
                    "site_url": item.get("site_url"),
                    "description": item.get("description")
                    or f"RSS feed covering {', '.join(themes) or 'current events'}.",
                    "match_reason": reason,
                    "access_level": item.get("access_level", "free_excerpt"),
                    "geographic_scope": geo_scope,
                    "sample_articles": item.get("sample_articles") or [],
                    "icon_url": item.get("icon_url")
                    or (
                        f"{item.get('site_url', '').rstrip('/')}/favicon.ico"
                        if item.get("site_url")
                        else None
                    ),
                }
            )

    if scope_level in ("city", "region"):
        local_only = [f for f in final_feeds if f.get("geographic_scope") in ("local", "regional")]
        if local_only:
            final_feeds = local_only

    await activity.emit(
        session,
        component="discovery",
        action="discover_done",
        detail={"count": len(final_feeds)},
    )
    await session.commit()
    return final_feeds
