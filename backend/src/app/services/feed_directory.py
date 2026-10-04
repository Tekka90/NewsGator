"""Client for the public feed directory used by feed discovery.

The directory exposes curated, language-filtered topic lists
(`/v3/recommendations/topics/<topic>`) and a keyword search over feed titles and
descriptions (`/v3/search/feeds`). Both endpoints are undocumented and rate limited, so every
request is spaced out, retried once on HTTP 429, and cached. They have no country filter,
only a language. Discovery must never depend on this service alone.
"""

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://cloud.feedly.com/v3"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) NewsGator/1.0"

# Minimum delay between two requests, backoff after a 429 and cache lifetime (seconds).
SPACING_S = 0.6
BACKOFF_S = 4.0
CACHE_TTL_S = 6 * 3600.0
REQUEST_TIMEOUT_S = 8.0

# The 13 discovery categories mapped to directory topic names. The directory resolves a
# topic name in any language, so the language is passed separately as `locale`.
CATEGORY_TOPICS: dict[str, str] = {
    "General News": "news",
    "World": "international",
    "Politics": "politics",
    "Finance": "finance",
    "Technology": "technology",
    "Science": "science",
    "Health": "health",
    "Environment": "environment",
    "Sports": "sports",
    "Culture": "culture",
    "Gaming": "gaming",
    "Food": "food",
    "Travel": "travel",
}

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_next_slot = 0.0
_slot_lock = asyncio.Lock()


def focus_topics(focus: str) -> list[str]:
    """Topic names to try for a free-text focus: the whole phrase first, then up to 3 words."""
    words = focus.lower().split()
    if not words:
        return []
    phrase = " ".join(words)
    if len(words) == 1:
        return [phrase]
    return [phrase, *[w for w in words if len(w) > 1][:3]]


async def _http_get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
    """Single GET request; module-level so tests can replace it."""
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=REQUEST_TIMEOUT_S
    ) as client:
        return await client.get(url, params=params)


async def _wait_for_slot() -> None:
    global _next_slot
    async with _slot_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + SPACING_S
    delay = slot - now
    if delay > 0:
        await asyncio.sleep(delay)


def parse_feeds(data: Any, key: str) -> list[dict[str, Any]]:
    """Normalize the directory's feed objects found under `key`."""
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    feeds: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("feedId"), str):
            continue
        feed_id = item["feedId"]
        feeds.append(
            {
                "feed_id": feed_id,
                "title": str(item.get("title") or "").strip(),
                "website": item.get("website") if isinstance(item.get("website"), str) else None,
                "description": str(item.get("description") or "").strip(),
                "subscribers": int(item.get("subscribers") or 0),
                "language": item.get("language") if isinstance(item.get("language"), str) else None,
                "icon_url": item.get("iconUrl") or item.get("visualUrl"),
            }
        )
    return feeds


async def _fetch(path: str, params: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    url = f"{BASE_URL}/{path}"
    cache_key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    hit = _cache.get(cache_key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    for attempt in range(2):
        await _wait_for_slot()
        try:
            resp = await _http_get(url, params)
        except Exception:
            return []
        if resp.status_code == 429:
            if attempt == 0:
                await asyncio.sleep(BACKOFF_S)
                continue
            return []
        if resp.status_code != 200:
            # Unknown topics answer 4xx; remember it so the same word is not retried.
            if 400 <= resp.status_code < 500:
                _cache[cache_key] = (time.monotonic(), [])
            return []
        try:
            feeds = parse_feeds(resp.json(), key)
        except ValueError:
            return []
        _cache[cache_key] = (time.monotonic(), feeds)
        return feeds
    return []


async def topic_feeds(topic: str, language: str | None) -> list[dict[str, Any]]:
    """Curated feeds for a topic in the given language; empty when the topic is unknown."""
    name = topic.strip()
    if not name:
        return []
    params = {"locale": language} if language else None
    return await _fetch(f"recommendations/topics/{quote(name, safe='')}", params, "feedInfos")


async def search_feeds(query: str, language: str | None, count: int) -> list[dict[str, Any]]:
    """Keyword search over feed titles and descriptions."""
    clean = query.strip()
    if not clean:
        return []
    params: dict[str, Any] = {"query": clean, "count": count}
    if language:
        params["locale"] = language
    return await _fetch("search/feeds", params, "results")


def clear_cache() -> None:
    """Forget cached answers (tests)."""
    _cache.clear()
