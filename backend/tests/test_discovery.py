"""Tests for LLM-driven feed discovery."""

import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.services import discovery, llm_client


async def test_discovery_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/api/feeds/discover", json={"themes": ["tech"]})
    assert r.status_code == 401


async def test_discovery_with_mocked_llm(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def fake_chat_json(system: str, user: str, model: str | None = None):
        if "discovery queries" in system.lower() or "search_queries" in user.lower():
            return {
                "search_queries": ["french cycling news rss"],
                "suggested_domains": ["cyclingnews.com"],
                "candidate_feed_urls": ["https://www.cyclingnews.com/feeds.xml"],
            }, 120
        elif "curate rss/atom feeds" in system.lower() or "verified live" in user.lower():
            return {
                "feeds": [
                    {
                        "url": "https://www.cyclingnews.com/feeds.xml",
                        "title": "Cyclingnews Pro",
                        "description": "Global racing and tech news.",
                        "match_reason": "Matches 'cycling news' in France.",
                    }
                ]
            }, 150
        return {}, 50

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    async def fake_feedsearch(domain: str):
        return ["https://www.cyclingnews.com/feeds.xml"]

    async def fake_probe(url: str):
        return {
            "url": url,
            "title": "Cyclingnews",
            "site_url": "https://www.cyclingnews.com",
            "description": "Latest cycling news",
            "sample_titles": ["Tour de France Stage 1", "Giro Preview"],
        }

    monkeypatch.setattr(discovery, "_search_feedsearch", fake_feedsearch)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={
            "location": "France",
            "themes": ["Sports & Athletics"],
            "query": "cycling news in France",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "feeds" in data
    assert len(data["feeds"]) >= 1
    first = data["feeds"][0]
    assert first["url"] == "https://www.cyclingnews.com/feeds.xml"
    assert "Cyclingnews" in first["title"]
    assert len(first["description"]) > 0
    assert len(first["match_reason"]) > 0


async def test_discovery_fallback_when_llm_fails(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def boom_chat_json(system: str, user: str, model: str | None = None):
        raise RuntimeError("LLM offline")

    monkeypatch.setattr(llm_client, "chat_json", boom_chat_json)

    # With search and probe mocked, it should still return validated candidates
    async def fake_probe(url: str):
        return {
            "url": url,
            "title": "Ars Technica",
            "site_url": "https://arstechnica.com",
            "description": "Technology news",
            "sample_titles": ["AI Chip Breakthrough"],
        }

    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={
            "location": "US",
            "themes": ["Tech & AI"],
            "query": "computing",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["feeds"]) >= 1
    assert data["feeds"][0]["title"] is not None


async def test_discovery_location_prioritizes_local_outlet(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def boom_chat_json(system: str, user: str, model: str | None = None):
        raise RuntimeError("LLM offline")

    monkeypatch.setattr(llm_client, "chat_json", boom_chat_json)

    async def fake_directory_search(
        location: str = "", themes: list[str] | None = None, query: str = ""
    ):
        return ["https://www.lyoncapitale.fr", "https://feeds.bbci.co.uk/news/world/rss.xml"]

    async def fake_probe(url: str):
        if "lyoncapitale" in url:
            return {
                "url": "https://www.lyoncapitale.fr/feed",
                "title": "Lyon Capitale",
                "site_url": "https://www.lyoncapitale.fr",
                "description": "Actualités lyonnaises et régionales",
                "sample_titles": ["Politique à Lyon", "Transports lyonnais"],
            }
        elif "bbci" in url:
            return {
                "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
                "title": "BBC News - World",
                "site_url": "https://www.bbc.com/news",
                "description": "International news",
                "sample_titles": ["Global summit"],
            }
        return None

    monkeypatch.setattr(discovery, "_search_directory_feeds", fake_directory_search)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={
            "location": "Lyon, France",
            "themes": ["General News"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    feeds = data["feeds"]
    assert len(feeds) == 1
    # Local publication should be kept, global/national excluded
    assert feeds[0]["title"] == "Lyon Capitale"
    assert feeds[0]["geographic_scope"] == "local"
    assert "Lyon" in feeds[0]["match_reason"]


async def test_discovery_detects_paywall_and_access_level(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def boom_chat_json(system: str, user: str, model: str | None = None):
        raise RuntimeError("LLM offline")

    monkeypatch.setattr(llm_client, "chat_json", boom_chat_json)

    async def fake_directory_search(
        location: str = "", themes: list[str] | None = None, query: str = ""
    ):
        return ["https://www.lemonde.fr/rss/une.xml", "https://feeds.arstechnica.com/arstechnica/index"]

    async def fake_probe(url: str):
        if "lemonde" in url:
            return {
                "url": url,
                "title": "Le Monde",
                "site_url": "https://www.lemonde.fr",
                "description": "Actualités nationales",
                "sample_titles": ["Article réservé aux abonnés: Enquête", "Chronique politique"],
                "access_level": "paywalled",
            }
        else:
            return {
                "url": url,
                "title": "Ars Technica",
                "site_url": "https://arstechnica.com",
                "description": "Tech news",
                "sample_titles": ["New GPU Benchmark"],
                "access_level": "free_full",
            }

    monkeypatch.setattr(discovery, "_search_directory_feeds", fake_directory_search)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={
            "location": "France",
            "themes": ["General News"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    feeds = data["feeds"]
    assert len(feeds) >= 2
    by_title = {f["title"]: f for f in feeds}
    assert by_title["Le Monde"]["access_level"] == "paywalled"
    assert by_title["Ars Technica"]["access_level"] == "free_full"
    assert by_title["Le Monde"]["geographic_scope"] == "national"


async def test_discovery_excluded_urls_filters_sources(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def boom_chat_json(system: str, user: str, model: str | None = None):
        raise RuntimeError("LLM offline")

    monkeypatch.setattr(llm_client, "chat_json", boom_chat_json)

    async def fake_directory_search(
        location: str = "", themes: list[str] | None = None, query: str = ""
    ):
        return [
            "https://www.lemonde.fr/rss/une.xml",
            "https://feeds.arstechnica.com/arstechnica/index",
            "https://techcrunch.com/feed/",
        ]

    async def fake_probe(url: str):
        if "lemonde" in url:
            return {
                "url": url,
                "title": "Le Monde",
                "site_url": "https://www.lemonde.fr",
                "description": "Actualités nationales",
                "sample_titles": ["Article 1"],
                "access_level": "paywalled",
            }
        elif "arstechnica" in url:
            return {
                "url": url,
                "title": "Ars Technica",
                "site_url": "https://arstechnica.com",
                "description": "Tech news",
                "sample_titles": ["Tech 1"],
                "access_level": "free_full",
            }
        elif "techcrunch" in url:
            return {
                "url": url,
                "title": "TechCrunch",
                "site_url": "https://techcrunch.com",
                "description": "Startup news",
                "sample_titles": ["Startup 1"],
                "access_level": "free_full",
            }
        return None

    monkeypatch.setattr(discovery, "_search_directory_feeds", fake_directory_search)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    # Exclude lemonde by URL and arstechnica by domain/URL
    resp = await client.post(
        "/api/feeds/discover",
        json={
            "location": "Global",
            "themes": ["Tech & AI"],
            "excluded_urls": [
                "https://www.lemonde.fr/rss/une.xml",
                "https://feeds.arstechnica.com/arstechnica/index",
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    feeds = data["feeds"]
    urls = [f["url"] for f in feeds]
    assert "https://www.lemonde.fr/rss/une.xml" not in urls
    assert "https://feeds.arstechnica.com/arstechnica/index" not in urls
    assert any("techcrunch" in u for u in urls)


async def test_discovery_catalog_mode(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    llm_called = False

    async def boom_llm(system: str, user: str, model: str | None = None):
        nonlocal llm_called
        llm_called = True
        raise RuntimeError("LLM should not be called in deterministic catalog search")

    monkeypatch.setattr(llm_client, "chat_json", boom_llm)

    async def fake_catalog(query: str, locale: str | None = None, count: int = 20):
        if "lyon" in query.lower():
            return [
                {
                    "feedId": "feed/http://www.lyoncapitale.fr/feed",
                    "title": "Lyon Capitale",
                    "website": "https://www.lyoncapitale.fr",
                    "subscribers": 1243,
                },
                {
                    "feedId": "feed/http://lyon.citycrunch.fr/feed/",
                    "title": "Lyon CityCrunch",
                    "website": "https://lyon.citycrunch.fr",
                    "subscribers": 463,
                },
            ]
        return []

    async def fake_probe(url: str):
        if "lyoncapitale" in url:
            return {
                "url": url,
                "title": "Lyon Capitale",
                "site_url": "https://www.lyoncapitale.fr",
                "description": "Lyon news",
                "sample_titles": ["Article 1"],
                "access_level": "free_full",
            }
        elif "citycrunch" in url:
            return {
                "url": url,
                "title": "Lyon CityCrunch",
                "site_url": "https://lyon.citycrunch.fr",
                "description": "Good places in Lyon",
                "sample_titles": ["Article 2"],
                "access_level": "free_excerpt",
            }
        return None

    monkeypatch.setattr(discovery, "_search_catalog", fake_catalog)
    monkeypatch.setattr(discovery, "_probe_url", fake_probe)

    resp = await client.post(
        "/api/feeds/discover",
        json={
            "location": "Lyon",
            "mode": "catalog",
            "locale": "fr_FR",
            "themes": ["news"],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert not llm_called
    feeds = data["feeds"]
    assert len(feeds) == 2
    # Ranked strictly by subscribers descending
    assert feeds[0]["title"] == "Lyon Capitale"
    assert feeds[1]["title"] == "Lyon CityCrunch"


