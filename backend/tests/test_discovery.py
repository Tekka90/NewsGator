"""Tests for feed discovery (curated topics, news mentions, AI suggestions)."""

import time
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.services import discovery, feed_directory, llm_client


def _entry(url: str, title: str, subs: int = 1000, lang: str = "fr") -> dict[str, Any]:
    return {
        "feed_id": f"feed/{url}",
        "title": title,
        "website": url.rsplit("/", 1)[0],
        "description": "",
        "subscribers": subs,
        "language": lang,
        "icon_url": None,
    }


def _probe_result(url: str, title: str, age_days: float = 1) -> dict[str, Any]:
    return {
        "url": url,
        "title": title,
        "site_url": url.rsplit("/", 1)[0],
        "description": "desc",
        "sample_titles": ["Un titre"],
        "sample_articles": [],
        "access_level": "free_full",
        "newest_ts": time.time() - age_days * 86400,
    }


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_news(term: str, language: str, country: str) -> dict[str, tuple[int, str]]:
        return {}

    monkeypatch.setattr(discovery, "_news_mentions", no_news)
    monkeypatch.setattr(llm_client, "is_configured", lambda: False)
    feed_directory.clear_cache()


def test_feed_key_normalizes() -> None:
    assert discovery.feed_key("http://www.Example.com/feed/#x") == discovery.feed_key(
        "https://example.com/feed"
    )


def test_focus_topics() -> None:
    assert feed_directory.focus_topics("Lyon") == ["lyon"]
    assert feed_directory.focus_topics("local news lyon") == [
        "local news lyon",
        "local",
        "news",
        "lyon",
    ]


async def test_discovery_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/api/feeds/discover", json={"themes": ["Technology"]})
    assert r.status_code == 401


async def test_category_uses_curated_topic_and_skips_excluded(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    calls: list[tuple[str, str | None]] = []

    async def fake_topic(topic: str, language: str | None) -> list[dict[str, Any]]:
        calls.append((topic, language))
        return [
            _entry("https://frandroid.com/feed", "Frandroid", 9000),
            _entry("http://www.lesnumeriques.com/rss/", "Les Numeriques", 5000),
            _entry("https://frandroid.com/feed/", "Frandroid dup", 10),
        ]

    async def fake_probe(url: str) -> dict[str, Any] | None:
        return _probe_result(url, "Probed")

    monkeypatch.setattr(feed_directory, "topic_feeds", fake_topic)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={
            "mode": "catalog",
            "themes": ["Technology"],
            "locale": "fr_FR",
            "excluded_urls": ["https://www.lesnumeriques.com/rss"],
        },
    )
    assert resp.status_code == 200, resp.text
    feeds = resp.json()["feeds"]
    assert calls == [("technology", "fr")]
    assert [f["url"] for f in feeds] == ["https://frandroid.com/feed"]
    assert feeds[0]["sources"] == ["directory"]


async def test_free_text_falls_back_to_keyword_search(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    searched: list[tuple[str, int]] = []

    async def no_topic(topic: str, language: str | None) -> list[dict[str, Any]]:
        return []

    async def fake_search(query: str, language: str | None, count: int) -> list[dict[str, Any]]:
        searched.append((query, count))
        return [_entry("https://leprogres.fr/feed", "Le Progres")]

    async def fake_probe(url: str) -> dict[str, Any] | None:
        return _probe_result(url, "Le Progres")

    monkeypatch.setattr(feed_directory, "topic_feeds", no_topic)
    monkeypatch.setattr(feed_directory, "search_feeds", fake_search)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={"mode": "catalog", "query": "lyon", "themes": [], "locale": "fr_FR"},
    )
    assert resp.status_code == 200, resp.text
    assert searched == [("lyon", 100)]
    assert [f["title"] for f in resp.json()["feeds"]] == ["Le Progres"]


async def test_stale_and_wrong_language_dropped(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def fake_topic(topic: str, language: str | None) -> list[dict[str, Any]]:
        return [
            _entry("https://a.fr/feed", "Fresh"),
            _entry("https://b.fr/feed", "Stale"),
            _entry("https://c.com/feed", "English", lang="en"),
        ]

    async def fake_probe(url: str) -> dict[str, Any] | None:
        return _probe_result(url, "x", age_days=400 if "b.fr" in url else 1)

    monkeypatch.setattr(feed_directory, "topic_feeds", fake_topic)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={"mode": "catalog", "themes": ["Science"], "locale": "fr_FR"},
    )
    assert [f["url"] for f in resp.json()["feeds"]] == ["https://a.fr/feed"]


async def test_news_mentions_add_publisher_leads(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def no_topic(topic: str, language: str | None) -> list[dict[str, Any]]:
        return []

    async def news(term: str, language: str, country: str) -> dict[str, tuple[int, str]]:
        return {"lemonde.fr": (5, "Le Monde")}

    async def fake_probe(url: str) -> dict[str, Any] | None:
        return _probe_result("https://lemonde.fr/rss", "Le Monde") if "lemonde" in url else None

    monkeypatch.setattr(feed_directory, "topic_feeds", no_topic)
    monkeypatch.setattr(discovery, "_news_mentions", news)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={"mode": "catalog", "themes": ["World"], "locale": "fr_FR"},
    )
    feeds = resp.json()["feeds"]
    assert [f["url"] for f in feeds] == ["https://lemonde.fr/rss"]
    assert feeds[0]["sources"] == ["news"]


async def test_smart_mode_uses_llm_suggestions(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(llm_client, "is_configured", lambda: True)

    async def fake_chat_json(
        system: str, user: str, *, model: str | None = None
    ) -> tuple[dict[str, Any], int]:
        if "fits a topic" in system:
            return {"covers": True}, 5
        return {
            "suggestions": [
                {"name": "Cyclingnews", "website": "http://cyclingnews.com", "feed_url": ""}
            ]
        }, 5

    async def fake_probe(url: str) -> dict[str, Any] | None:
        return _probe_result("https://cyclingnews.com/feed", "Cyclingnews")

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={"mode": "smart", "query": "cycling", "locale": "fr_FR"},
    )
    assert resp.status_code == 200, resp.text
    feeds = resp.json()["feeds"]
    assert [f["url"] for f in feeds] == ["https://cyclingnews.com/feed"]
    assert feeds[0]["sources"] == ["ai"]


async def test_directory_retries_once_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_directory.clear_cache()
    monkeypatch.setattr(feed_directory, "SPACING_S", 0.0)
    monkeypatch.setattr(feed_directory, "BACKOFF_S", 0.0)
    statuses = [429, 200]
    seen: list[int] = []

    async def fake_get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        status = statuses.pop(0)
        seen.append(status)
        body = {"feedInfos": [{"feedId": "feed/https://a.fr/feed", "title": "A"}]}
        return httpx.Response(status, json=body if status == 200 else {})

    monkeypatch.setattr(feed_directory, "_http_get", fake_get)
    feeds = await feed_directory.topic_feeds("technology", "fr")
    assert seen == [429, 200]
    assert [f["feed_id"] for f in feeds] == ["feed/https://a.fr/feed"]
