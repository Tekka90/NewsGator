"""LLM-driven feed discovery service.

Supports both local LLMs and OpenAI-compatible cloud endpoints.
Flow:
1. Turn 1 (Query Formulation): LLM emits search queries, target domains, candidate URLs.
2. Search & Discovery: Executes web queries (e.g. DuckDuckGo lite / curated directory),
   scrapes target HTML for <link rel="alternate" type="application/rss+xml">.
3. Live Validation: Probes candidate URLs with feedparser via anyio.to_thread to
   ensure only active, working RSS/Atom feeds are returned.
4. Turn 2 (Synthesis & Ranking): LLM annotates validated feeds with personalized
   descriptions and match reasons tailored to the user's criteria.
"""

import asyncio
import concurrent.futures
import re
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

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

# Curated high-quality seed feeds by category to augment web search & guarantee
# reliable discovery results across standard themes & regions even when offline.
CURATED_SEEDS: dict[str, list[dict[str, str]]] = {
    "tech": [
        {
            "title": "Ars Technica",
            "url": "https://feeds.arstechnica.com/arstechnica/index",
            "site_url": "https://arstechnica.com",
            "desc": "Original tech reporting, reviews, and analysis.",
        },
        {
            "title": "Hacker News Frontpage",
            "url": "https://news.ycombinator.com/rss",
            "site_url": "https://news.ycombinator.com",
            "desc": "Technology, startup, and computer science community news.",
        },
        {
            "title": "The Verge",
            "url": "https://www.theverge.com/rss/index.xml",
            "site_url": "https://www.theverge.com",
            "desc": "Technology, science, art, and European tech culture.",
        },
        {
            "title": "AnandTech / Tom's Hardware",
            "url": "https://www.tomshardware.com/feeds/all",
            "site_url": "https://www.tomshardware.com",
            "desc": "Hardware reviews, PC components, benchmarks, and semiconductors.",
        },
    ],
    "general": [
        {
            "title": "BBC News - World",
            "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
            "site_url": "https://www.bbc.com/news",
            "desc": "International breaking news and global analysis from the BBC.",
        },
        {
            "title": "Reuters World News",
            "url": "https://www.reutersagency.com/feed/?best-topics=world&post_type=best",
            "site_url": "https://www.reuters.com",
            "desc": "Unbiased global breaking news, business, and investigative reporting.",
        },
        {
            "title": "NPR News",
            "url": "https://feeds.npr.org/1001/rss.xml",
            "site_url": "https://www.npr.org",
            "desc": "Independent, fact-based news coverage and in-depth journalism.",
        },
    ],
    "science": [
        {
            "title": "Nature - Latest Research",
            "url": "https://www.nature.com/nature.rss",
            "site_url": "https://www.nature.com",
            "desc": "Leading international peer-reviewed weekly scientific journal.",
        },
        {
            "title": "ScienceDaily",
            "url": "https://www.sciencedaily.com/rss/all.xml",
            "site_url": "https://www.sciencedaily.com",
            "desc": "Breaking science news and research articles from universities and journals.",
        },
        {
            "title": "Phys.org",
            "url": "https://phys.org/rss-feed/",
            "site_url": "https://phys.org",
            "desc": "Physics, space exploration, nanotechnology, and earth sciences.",
        },
    ],
    "research": [
        {
            "title": "arXiv Computer Science",
            "url": "https://rss.arxiv.org/rss/cs",
            "site_url": "https://arxiv.org",
            "desc": "Daily computer science preprint submissions from Cornell University.",
        },
        {
            "title": "MIT Technology Review",
            "url": "https://www.technologyreview.com/feed/",
            "site_url": "https://www.technologyreview.com",
            "desc": "In-depth research insights on emerging technologies and societal impact.",
        },
    ],
    "gaming": [
        {
            "title": "IGN",
            "url": "https://feeds.feedburner.com/ign/all",
            "site_url": "https://www.ign.com",
            "desc": "Video game news, reviews, guides, and trailers across all platforms.",
        },
        {
            "title": "Polygon",
            "url": "https://www.polygon.com/rss/index.xml",
            "site_url": "https://www.polygon.com",
            "desc": "Gaming culture, features, reviews, and entertainment coverage.",
        },
        {
            "title": "Eurogamer",
            "url": "https://www.eurogamer.net/feed",
            "site_url": "https://www.eurogamer.net",
            "desc": "European video game news, reviews, digital foundry technical analysis.",
        },
    ],
    "business": [
        {
            "title": "Financial Times - World",
            "url": "https://www.ft.com/rss/home/world",
            "site_url": "https://www.ft.com",
            "desc": "Global business, financial market trends, and economic analysis.",
        },
        {
            "title": "Bloomberg Markets",
            "url": "https://feeds.bloomberg.com/markets/news.rss",
            "site_url": "https://www.bloomberg.com",
            "desc": "Financial market updates, corporate earnings, and commodities.",
        },
        {
            "title": "The Economist",
            "url": "https://www.economist.com/rss/the_world_this_week_rss.xml",
            "site_url": "https://www.economist.com",
            "desc": "International business, economic policy, and political commentary.",
        },
    ],
    "politics": [
        {
            "title": "Politico",
            "url": "https://www.politico.com/rss/politicopicks.xml",
            "site_url": "https://www.politico.com",
            "desc": "Political reporting, election analysis, legislation, and public policy.",
        },
        {
            "title": "Le Monde International",
            "url": "https://www.lemonde.fr/international/rss_full.xml",
            "site_url": "https://www.lemonde.fr",
            "desc": "In-depth geopolitical reporting and European political analysis.",
        },
    ],
    "climate": [
        {
            "title": "Inside Climate News",
            "url": "https://insideclimatenews.org/feed/",
            "site_url": "https://insideclimatenews.org",
            "desc": "Pulitzer-winning journalism on climate, environmental policy and science.",
        },
        {
            "title": "Yale Environment 360",
            "url": "https://e360.yale.edu/feed",
            "site_url": "https://e360.yale.edu",
            "desc": "Analysis, reporting, and debate on global environmental issues.",
        },
    ],
    "security": [
        {
            "title": "Krebs on Security",
            "url": "https://krebsonsecurity.com/feed/",
            "site_url": "https://krebsonsecurity.com",
            "desc": "In-depth investigative journalism on cybersecurity and cybercrime.",
        },
        {
            "title": "BleepingComputer",
            "url": "https://www.bleepingcomputer.com/feed/",
            "site_url": "https://www.bleepingcomputer.com",
            "desc": "Information security news, ransomware alerts, and vulnerability advisories.",
        },
        {
            "title": "Schneier on Security",
            "url": "https://www.schneier.com/feed/atom/",
            "site_url": "https://www.schneier.com",
            "desc": "Security, privacy, cryptography essays and policy by Bruce Schneier.",
        },
    ],
    "sports": [
        {
            "title": "Cyclingnews",
            "url": "https://www.cyclingnews.com/feeds.xml",
            "site_url": "https://www.cyclingnews.com",
            "desc": "Global cycling races, Tour de France, results, gear and pro peloton news.",
        },
        {
            "title": "The Athletic",
            "url": "https://theathletic.com/rss/news/",
            "site_url": "https://theathletic.com",
            "desc": "In-depth sports journalism, live coverage, and player analysis.",
        },
        {
            "title": "L'Équipe",
            "url": "https://dwh.lequipe.fr/api/edito/rss?path=/",
            "site_url": "https://www.lequipe.fr",
            "desc": "French and European sports daily covering football, cycling, and tennis.",
        },
    ],
}


async def _search_google_news_rss(
    query: str, location: str = "", max_results: int = 10
) -> list[str]:
    """Query Google News RSS to discover local regional publications and working topic feeds."""
    combined = f"{location} {query}".lower()
    if any(
        k in combined
        for k in (
            "franc", "lyon", "paris", "marseille", "bordeaux", "toulouse", "lille",
            "nantes", "strasbourg", "nice", "actualit"
        )
    ):
        hl, gl, ceid = "fr", "FR", "FR:fr"
    elif any(k in combined for k in ("german", "deutsch", "berlin", "münchen", "munich", "hamburg")):
        hl, gl, ceid = "de", "DE", "DE:de"
    elif any(k in combined for k in ("spain", "españ", "madrid", "barcelona")):
        hl, gl, ceid = "es", "ES", "ES:es"
    elif any(k in combined for k in ("ital", "roma", "milan")):
        hl, gl, ceid = "it", "IT", "IT:it"
    elif any(k in combined for k in ("uk", "london", "england")):
        hl, gl, ceid = "en-GB", "GB", "GB:en"
    else:
        hl, gl, ceid = "en-US", "US", "US:en"

    # Clean query to avoid RSS technical buzzwords reducing article hits
    clean_q = query
    for kw in ("flux rss", "rss feed", "rss", "feed"):
        clean_q = re.sub(rf"\b{kw}\b", "", clean_q, flags=re.IGNORECASE)
    clean_q = clean_q.strip() or location or "top news"

    urls: list[str] = []
    gnews_url = f"https://news.google.com/rss/search?q={quote_plus(clean_q)}&hl={hl}&gl={gl}&ceid={ceid}"

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=8.0,
        ) as client:
            resp = await client.get(gnews_url)
            if resp.status_code == 200 and resp.text:
                sources = re.findall(
                    r'<source[^>]*url=["\']([^"\']+)["\']', resp.text, flags=re.IGNORECASE
                )
                for src in sources:
                    if src.startswith("http") and not any(
                        x in src for x in ("google.com", "youtube.com", "duckduckgo.com", "bing.com", "yahoo.com")
                    ):
                        if src not in urls:
                            urls.append(src)
                            if len(urls) >= max_results:
                                break
    except Exception:
        pass
    return urls


def _clean_feed_title(raw: str) -> str:
    """Normalize verbose RSS feed titles into clean publication display names."""
    trimmed = raw.strip()
    if "le progrès" in trimmed.lower() or "le progres" in trimmed.lower():
        return "Le Progrès"
    if " | " in trimmed:
        parts = trimmed.split(" | ")
        if len(parts[-1].strip()) < len(parts[0].strip()) and len(parts[-1].strip()) >= 3:
            return parts[-1].strip()
    if " : " in trimmed:
        prefix = trimmed.split(" : ")[0].strip()
        if 3 <= len(prefix) <= 35:
            return prefix
    return trimmed


def _expand_location_aliases(location: str) -> list[str]:
    """Expand prominent cities/regions to their associated department, metropolitan or regional names."""
    lower = location.lower()
    aliases: list[str] = []
    if "lyon" in lower:
        aliases.extend(["rhone", "rhône", "auvergne-rhone-alpes"])
    elif "paris" in lower:
        aliases.extend(["ile-de-france", "idf"])
    elif "marseille" in lower:
        aliases.extend(["provence", "bouches-du-rhone", "paca"])
    elif "bordeaux" in lower:
        aliases.extend(["gironde", "aquitaine"])
    elif "toulouse" in lower:
        aliases.extend(["haute-garonne", "occitanie"])
    elif "lille" in lower:
        aliases.extend(["hauts-de-france", "nord"])
    elif "nantes" in lower:
        aliases.extend(["loire-atlantique", "pays-de-la-loire"])
    elif "strasbourg" in lower:
        aliases.extend(["alsace", "bas-rhin"])
    elif "nice" in lower:
        aliases.extend(["alpes-maritimes", "azur"])
    elif "rennes" in lower:
        aliases.extend(["bretagne", "ille-et-vilaine"])
    elif "grenoble" in lower:
        aliases.extend(["isere", "isère"])
    elif "saint-etienne" in lower or "saint-étienne" in lower:
        aliases.extend(["loire"])
    elif "clermont" in lower:
        aliases.extend(["auvergne", "puy-de-dome"])
    elif "montpellier" in lower:
        aliases.extend(["herault", "hérault", "occitanie"])
    elif "munich" in lower or "münchen" in lower:
        aliases.extend(["bayern", "bavaria"])
    elif "barcelona" in lower:
        aliases.extend(["catalunya", "catalonia"])
    elif "madrid" in lower:
        aliases.extend(["comunidad de madrid"])
    return aliases


async def _search_web_duckduckgo_lite(query: str, max_results: int = 6) -> list[str]:
    """Execute a web search using DuckDuckGo Lite to locate candidate sites and feeds."""
    urls: list[str] = []
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=True,
            timeout=8.0,
        ) as client:
            resp = await client.post("https://lite.duckduckgo.com/lite/", data={"q": query})
            if resp.status_code == 200 and resp.text:
                tree = lxml_html.fromstring(resp.text)
                for link in tree.xpath('//a[@class="result-link"]/@href'):
                    if isinstance(link, str) and link.startswith("http"):
                        # Exclude obvious non-feed/social redirects
                        if not any(
                            x in link for x in ("duckduckgo.com", "facebook.com", "instagram.com")
                        ):
                            urls.append(link)
                            if len(urls) >= max_results:
                                break
    except Exception:
        pass
    return urls


def _detect_paywall_markers(text: str) -> bool:
    """Look for standard paywall and subscription markers."""
    return bool(
        re.search(
            r"\b(abonn[ée]s?|subscribers?|paywall|r[ée]serv[ée] aux abonn[ée]s|edition abonn[ée]|premium)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


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
        if any(x in parsed_u.netloc.lower() for x in ("google.", "duckduckgo.", "bing.", "yahoo.", "youtube.")):
            return None

        parsed = feedparser.parse(url)
        # Check if feed contains valid structure
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

        # Concurrently inspect sample article web pages to verify paywalls with immediate short-circuit
        if not has_paywall_marker and sample_links:
            valid_links = [l for l in sample_links[:5] if isinstance(l, str) and l.startswith("http")]
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

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(valid_links), 5)) as executor:
                    futures = [executor.submit(_check_link, link) for link in valid_links]
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            if future.result():
                                has_paywall_marker = True
                                break
                        except Exception:
                            pass

        has_full_text = bool(content_lengths and (sum(content_lengths) / len(content_lengths)) >= 800)
        access_level = "paywalled" if has_paywall_marker else ("free_full" if has_full_text else "free_excerpt")

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
    # First test if the URL itself is already a valid feed
    res = await anyio.to_thread.run_sync(_parse_feed_sync, url, False)
    if res is not None:
        return res

    # If not directly a feed, try HTML autodiscovery
    parsed_u = urlparse(url)
    if not parsed_u.netloc or any(
        x in parsed_u.netloc.lower()
        for x in ("google.", "duckduckgo.", "bing.", "yahoo.", "youtube.")
    ):
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
                # Schema.org declarative paywall check
                if re.search(r'["\']isAccessibleForFree["\']\s*:\s*false', resp.text, flags=re.IGNORECASE):
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
                    cand_res = await anyio.to_thread.run_sync(_parse_feed_sync, candidate, html_paywalled)
                    if cand_res is not None:
                        return cand_res
    except Exception:
        pass

    # Common feed path suffixes fallback
    base = f"{parsed_u.scheme}://{parsed_u.netloc}"
    for path in ("/feed", "/rss", "/feed.xml", "/rss.xml", "/index.xml"):
        cand_url = urljoin(base, path)
        cand_res = await anyio.to_thread.run_sync(_parse_feed_sync, cand_url, html_paywalled)
        if cand_res is not None:
            return cand_res

    return None


def _theme_key(theme_name: str) -> str:
    """Normalize user theme category name to match curated seeds."""
    t = theme_name.lower()
    if "tech" in t or "ai" in t or "hardware" in t or "design" in t:
        return "tech"
    if "gaming" in t:
        return "gaming"
    if "science" in t or "nature" in t:
        return "science"
    if "research" in t or "academic" in t:
        return "research"
    if "business" in t or "finance" in t:
        return "business"
    if "politic" in t:
        return "politics"
    if "climate" in t or "environment" in t:
        return "climate"
    if "security" in t or "cyber" in t:
        return "security"
    if "sport" in t or "athletic" in t:
        return "sports"
    return "general"


async def discover_feeds(
    session: AsyncSession,
    *,
    location: str = "",
    themes: list[str] | None = None,
    query: str = "",
    excluded_urls: list[str] | None = None,
    lang_code: str | None = None,
) -> list[dict[str, Any]]:
    """Execute 2-turn LLM feed discovery with live HTTP/XML verification."""
    themes = themes or []
    loc_clean = location.strip()
    q_clean = query.strip()
    excluded_set = set(excluded_urls or [])
    excluded_domains = {urlparse(u).netloc.lower() for u in excluded_set if urlparse(u).netloc}

    await activity.emit(
        session,
        component="discovery",
        action="discover_start",
        detail={"location": loc_clean, "themes": themes, "query": q_clean},
    )

    # --- Turn 1: Query Formulation via LLM ---
    queries_to_run: list[str] = []
    candidate_urls: list[str] = []
    suggested_domains: list[str] = []

    is_global = not loc_clean or loc_clean.lower() in ("global", "worldwide")
    if is_global:
        scope_level = "global"
    elif loc_clean.lower() in (
        "france", "germany", "spain", "italy", "united states", "usa", "uk", "canada", "japan"
    ):
        scope_level = "country"
    elif "," in loc_clean:
        scope_level = "city"
    else:
        scope_level = "city"

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
            if parsed_q.get("scope_level") in ("city", "region", "country", "continent", "global"):
                scope_level = parsed_q["scope_level"]
            for q in parsed_q.get("search_queries", []):
                if isinstance(q, str) and q.strip():
                    queries_to_run.append(q.strip())
            for d in parsed_q.get("suggested_domains", []):
                if isinstance(d, str) and d.strip():
                    suggested_domains.append(d.strip())
            for u in parsed_q.get("candidate_feed_urls", []):
                if isinstance(u, str) and u.strip().startswith("http"):
                    candidate_urls.append(u.strip())
    except Exception:
        # If LLM Turn 1 fails (e.g. offline/timeout), fallback to heuristic queries
        if q_clean:
            queries_to_run.append(f"{q_clean} rss feed")
        for t in themes:
            queries_to_run.append(f"{t} {loc_clean} news rss feed")

    # If no queries generated, construct default queries
    if not queries_to_run:
        base_term = " ".join(themes) if themes else "news"
        queries_to_run.append(f"{base_term} {loc_clean} rss feed".strip())

    # --- Search & Web Discovery ---
    loc_parts = [p.strip() for p in loc_clean.split(",") if p.strip()]
    primary_city = loc_parts[0] if loc_parts else loc_clean
    city_words = [w for w in re.findall(r"\w+", primary_city.lower()) if len(w) >= 3]
    regional_aliases = _expand_location_aliases(loc_clean)

    if not is_global:
        lower_loc = loc_clean.lower()
        if any(w in lower_loc for w in ("france", "lyon", "paris", "marseille", "bordeaux", "toulouse", "lille", "nantes", "rennes", "strasbourg", "nice")):
            queries_to_run.extend([
                f"presse {primary_city}",
                f"{primary_city} journal quotidien",
                f"{primary_city} presse régionale",
            ])
        elif any(w in lower_loc for w in ("germany", "deutschland", "berlin", "münchen", "munich", "hamburg", "köln")):
            queries_to_run.extend([
                f"presse zeitung {primary_city}",
                f"{primary_city} tageszeitung nachrichten",
            ])
        elif any(w in lower_loc for w in ("spain", "españa", "madrid", "barcelona", "valencia", "sevilla")):
            queries_to_run.extend([
                f"prensa periodico {primary_city}",
                f"{primary_city} diario noticias local",
            ])
        else:
            queries_to_run.extend([
                f"local newspaper {primary_city}",
                f"{primary_city} daily news",
            ])

    # Probe suggested candidate feed URLs
    probe_targets: set[str] = set(candidate_urls)

    for d in suggested_domains:
        if not d.startswith("http"):
            probe_targets.add(f"https://{d}")
        else:
            probe_targets.add(d)

    # Run searches for top queries via Google News RSS and DuckDuckGo
    search_tasks = []
    for q in queries_to_run[:4]:
        search_tasks.append(_search_google_news_rss(q, location=loc_clean, max_results=8))
        search_tasks.append(_search_web_duckduckgo_lite(q, max_results=5))

    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
    for res in search_results:
        if isinstance(res, list):
            for url in res:
                if not any(x in url.lower() for x in ("google.", "duckduckgo.", "bing.", "yahoo.", "youtube.")):
                    probe_targets.add(url)

    # Add curated seeds for selected themes (only upfront if global search)
    theme_keys = {_theme_key(t) for t in themes} if themes else {"general"}
    curated_candidates: list[dict[str, Any]] = []
    for k in theme_keys:
        for seed in CURATED_SEEDS.get(k, []):
            curated_candidates.append(seed)
            if is_global:
                probe_targets.add(seed["url"])

    # Filter out already excluded targets and domains
    filtered_targets = [
        u for u in probe_targets
        if u not in excluded_set and urlparse(u).netloc.lower() not in excluded_domains
    ]

    # Prioritize targets matching city or regional keywords
    def _target_priority(u: str) -> int:
        u_lower = u.lower()
        score = 0
        for w in city_words:
            if w in u_lower:
                score += 50
        for a in regional_aliases:
            if a in u_lower:
                score += 40
        if any(k in u_lower for k in ("presse", "journal", "actualite", "quotidien", "tribune", "gazette", "capitale")):
            score += 25
        return score

    sorted_targets = sorted(filtered_targets, key=_target_priority, reverse=True)

    # --- Live Verification Step ---
    # Probe candidate targets concurrently with a semaphore
    sem = asyncio.Semaphore(6)
    validated_feeds: list[dict[str, Any]] = []
    seen_urls: set[str] = set(excluded_set)

    async def _safe_probe(u: str) -> None:
        async with sem:
            try:
                res = await _probe_url(u)
                if res and res["url"] not in seen_urls:
                    res_domain = urlparse(res["url"]).netloc.lower()
                    if res_domain not in excluded_domains:
                        seen_urls.add(res["url"])
                        validated_feeds.append(res)
            except Exception:
                pass

    probe_tasks = [_safe_probe(u) for u in sorted_targets[:30]]
    await asyncio.gather(*probe_tasks, return_exceptions=True)

    # Guarantee seeds ONLY if global or if zero local feeds could be discovered
    if is_global and len(validated_feeds) < 3:
        for seed in curated_candidates:
            if seed["url"] not in seen_urls:
                seen_urls.add(seed["url"])
                validated_feeds.append(
                    {
                        "url": seed["url"],
                        "title": seed["title"],
                        "site_url": seed["site_url"],
                        "description": seed["desc"],
                        "sample_titles": [],
                    }
                )
    elif not validated_feeds:
        for seed in curated_candidates:
            if seed["url"] not in seen_urls:
                seen_urls.add(seed["url"])
                validated_feeds.append(
                    {
                        "url": seed["url"],
                        "title": seed["title"],
                        "site_url": seed["site_url"],
                        "description": seed["desc"],
                        "sample_titles": [],
                        "_is_fallback": True,
                    }
                )

    # Relevance scoring and ranking
    country_words = [
        w for w in re.findall(r"\w+", loc_clean.lower()) if len(w) >= 3 and w not in city_words
    ]
    q_words = [w for w in re.findall(r"\w+", q_clean.lower()) if len(w) >= 2]

    def _location_score(feed: dict[str, Any]) -> int:
        t = (feed.get("title") or "").lower()
        d = (feed.get("description") or "").lower()
        u = (feed.get("url") or "").lower()
        samples = " ".join(feed.get("sample_titles") or []).lower()
        score = 0

        # High-weight boost for user custom query terms
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

        # General news daily press indicator boost
        if any(x in t or x in u for x in ("quotidien", "journal", "tribune", "gazette", "presse")):
            score += 35

        if is_global:
            return score

        for w in city_words:
            if w in t:
                score += 50
            if w in u:
                score += 40
            if w in samples:
                score += 35
            if w in d:
                score += 20
        for a in regional_aliases:
            if a in t:
                score += 45
            if a in u:
                score += 35
            if a in samples:
                score += 30
            if a in d:
                score += 15
        if any(x in t for x in ("région", "region", "locale", "local")):
            score += 15
        for w in country_words:
            if w in t:
                score += 5
            if w in u:
                score += 5
            if w in samples:
                score += 3
            if w in d:
                score += 2
        return score

    validated_feeds.sort(key=_location_score, reverse=True)
    # Limit to top 10 verified candidates for synthesis
    validated_top = validated_feeds[:10]

    # --- Turn 2: Synthesis & Ranking via LLM ---
    final_feeds: list[dict[str, Any]] = []

    if validated_top:
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
                            "local" if _location_score(base) >= 20 else "national"
                        )
                        if geo not in ("local", "regional", "national", "global"):
                            geo = "local" if _location_score(base) >= 20 else "national"

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
                                "icon_url": f"{base.get('site_url', '').rstrip('/')}/favicon.ico"
                                if base.get("site_url")
                                else None,
                            }
                        )
        except Exception:
            pass

    # Heuristic fallback if LLM synthesis failed or returned empty
    if not final_feeds:
        for item in validated_top:
            if item.get("_is_fallback"):
                reason = f"General news fallback (no local feeds found for {loc_clean})"
                geo_scope = "global"
            else:
                score = _location_score(item)
                if score >= 20:
                    reason = f"Local publication covering {loc_parts[0]}"
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
                    "icon_url": f"{item.get('site_url', '').rstrip('/')}/favicon.ico"
                    if item.get("site_url")
                    else None,
                }
            )

    # When searching for a specific city/region, filter out generic national/global outlets
    if scope_level in ("city", "region"):
        local_only = [
            f for f in final_feeds if f.get("geographic_scope") in ("local", "regional")
        ]
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
