"""Coverage tests: discovery helpers (probing, leads, scoring). All network is mocked."""

import time
from typing import Any

import feedparser
import httpx
import pytest

from app.services import discovery, llm_client, youtube

_REAL_ASYNC = httpx.AsyncClient
_REAL_SYNC = httpx.Client


def _rss(
    items: list[dict[str, str]], title: str = "My Feed", link: str = "https://site.test"
) -> str:
    body = "".join(
        f"<item><title>{i.get('title', '')}</title><link>{i.get('link', '')}</link>"
        f"<description>{i.get('desc', '')}</description>"
        f"<pubDate>{i.get('date', 'Mon, 01 Jan 2024 10:00:00 GMT')}</pubDate></item>"
        for i in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>'
        f"<link>{link}</link><description>Sub</description>{body}</channel></rss>"
    )


@pytest.fixture()
def net(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Route httpx + feedparser through in-memory tables.

    `pages[url] = (status, content_type, text)`; `feeds[url] = rss xml`; `calls` records requests.
    """
    state: dict[str, Any] = {"pages": {}, "feeds": {}, "calls": [], "boom": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        state["calls"].append(url)
        base = url.split("?")[0]
        if base in state["boom"]:
            raise httpx.ConnectError("refused")
        status, ctype, text = state["pages"].get(url) or state["pages"].get(
            base, (404, "text/plain", "")
        )
        return httpx.Response(status, headers={"content-type": ctype}, text=text)

    def make_async(**kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC(**kw)

    def make_sync(**kw: Any) -> httpx.Client:
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_SYNC(**kw)

    real_parse = feedparser.parse
    monkeypatch.setattr(discovery.httpx, "AsyncClient", make_async)
    monkeypatch.setattr(discovery.httpx, "Client", make_sync)
    monkeypatch.setattr(
        discovery.feedparser,
        "parse",
        lambda u: real_parse(state["feeds"].get(u, u if u.lstrip().startswith("<") else "")),
    )
    return state


# --- pure helpers ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Plain Title  ", "Plain Title"),
        ("Very long section headline | Le Monde", "Le Monde"),
        ("Short | A much longer trailing part", "Short | A much longer trailing part"),
        ("Short | AB", "Short | AB"),
        ("Brand : tagline words here", "Brand"),
        ("X : tiny prefix too short", "X : tiny prefix too short"),
    ],
)
def test_clean_feed_title(raw: str, expected: str) -> None:
    assert discovery._clean_feed_title(raw) == expected


def test_paywall_markers_and_html() -> None:
    assert discovery._detect_paywall_markers("Réservé aux abonnés")
    assert discovery._detect_paywall_markers("Premium content")
    assert not discovery._detect_paywall_markers("subscriptions are great")  # word boundary
    assert discovery._check_html_paywall('{"isAccessibleForFree": false}')
    assert discovery._check_html_paywall("<div class='x article-paywall y'>")
    assert not discovery._check_html_paywall('{"isAccessibleForFree": true}')
    assert not discovery._check_html_paywall("<div class='paywalled-not'>")


def test_path_affinity_counts_shared_leading_segments() -> None:
    assert discovery._path_affinity("https://a/politik/feed.xml", "https://a/politik/story-1") == 1
    assert discovery._path_affinity("https://a/sport/x/feed.xml", "https://a/sport/y/z") == 1
    assert discovery._path_affinity("https://a/feed.xml", "https://a/politik/story") == 0
    assert discovery._path_affinity("https://a/a/b/feed", "https://a/a/b/c") == 2


def test_site_key_and_blocked_hosts() -> None:
    assert discovery.site_key("https://www.Example.com/x") == "example.com"
    assert discovery.site_key("WWW.Example.com") == "example.com"
    for host in ("news.google.com", "x.com", "m.x.com", "old.reddit.com", ""):
        assert discovery._blocked(host)
    assert not discovery._blocked("lemonde.fr")
    assert discovery._https("http://a.b") == "https://a.b"


def test_directory_lead_filters_and_normalizes() -> None:
    lead = discovery._directory_lead(
        {
            "feed_id": "feed/http://a.test/rss",
            "title": "A",
            "website": "http://a.test",
            "subscribers": 5,
            "language": "FR-ca",
            "icon_url": "i.png",
        },
        curated=True,
    )
    assert lead is not None
    assert (lead.feed_url, lead.site_url, lead.language, lead.icon_url) == (
        "https://a.test/rss",
        "https://a.test",
        "fr",
        "i.png",
    )
    assert lead.curated and lead.sources == {"directory"}
    no_site = discovery._directory_lead({"feed_id": "https://b.test/f"}, curated=False)
    assert no_site is not None and no_site.site_url == "https://b.test" and no_site.language is None
    assert discovery._directory_lead({"feed_id": "feed/ftp://c/f"}, True) is None
    assert discovery._directory_lead({"feed_id": "feed/https://news.google.com/x"}, True) is None
    assert discovery._directory_lead({}, True) is None


def test_pre_score_weights() -> None:
    both = discovery._Lead(
        site_url="s",
        sources={"directory", "news"},
        mentions=2,
        terms_matched=1,
        subscribers=999_999,
    )
    only = discovery._Lead(
        site_url="s", sources={"directory"}, mentions=0, terms_matched=1, subscribers=0
    )
    assert discovery._pre_score(both, 2, 1) == pytest.approx(
        1.0 + 1.5 + 1.0 + 3.0 * (6 / 6), rel=1e-3
    )
    assert discovery._pre_score(only, 2, 2) == pytest.approx(0.5)
    assert discovery._pre_score(both, 2, 1) > discovery._pre_score(only, 2, 1)


def test_language_matches() -> None:
    lead = discovery._Lead(site_url="s", language="fr")
    assert discovery._language_matches(lead, {}, None)
    assert discovery._language_matches(lead, {}, "fr")
    assert not discovery._language_matches(lead, {}, "de")
    unknown = discovery._Lead(site_url="s")
    assert discovery._language_matches(
        unknown, {"sample_titles": ["short"]}, "de"
    )  # too little text
    english = {
        "sample_titles": [
            "The council voted on Tuesday to approve the plan",
            "Markets rose sharply",
        ]
    }
    assert discovery._language_matches(unknown, english, "en")
    assert not discovery._language_matches(unknown, english, "fr")
    assert discovery._language_matches(
        unknown, {"sample_titles": ["1234 5678 9012 3456 7890"]}, "fr"
    )


def test_country_name() -> None:
    assert discovery._country_name("en", None) is None
    assert discovery._country_name("en", "FR") == "France"
    assert discovery._country_name("fr", "DE") == "Allemagne"


# --- network helpers ---


async def test_search_feedsearch(net) -> None:
    net["pages"]["https://feedsearch.dev/api/v1/search?url=site.test"] = (
        200,
        "application/json",
        '[{"url": "https://site.test/rss"}, {"x": 1}, "junk"]',
    )
    assert await discovery._search_feedsearch("https://site.test/some/page") == [
        "https://site.test/rss"
    ]
    assert await discovery._search_feedsearch("site.test") == ["https://site.test/rss"]
    assert await discovery._search_feedsearch("  ") == []
    net["pages"]["https://feedsearch.dev/api/v1/search?url=obj.test"] = (
        200,
        "application/json",
        '{"a": 1}',
    )
    assert await discovery._search_feedsearch("obj.test") == []
    assert await discovery._search_feedsearch("missing.test") == []  # 404
    net["boom"].add("https://feedsearch.dev/api/v1/search")
    assert await discovery._search_feedsearch("site.test") == []


async def test_feedsearch_feeds_sorted_and_limited(net) -> None:
    payload = (
        '[{"url": "https://s.test/low", "score": 1, "title": "Long section headline | Big Name"},'
        ' {"url": "https://s.test/high", "score": 9, "title": "", "description": "d"},'
        ' {"title": "no url"}]'
    )
    net["pages"]["https://feedsearch.dev/api/v1/search?url=s.test&info=true"] = (
        200,
        "application/json",
        payload,
    )
    out = await discovery._feedsearch_feeds("https://s.test/page")
    assert [f["url"] for f in out] == ["https://s.test/high", "https://s.test/low"]
    assert out[0]["title"] == "s.test" and out[0]["description"] == "d"
    assert out[0]["site_url"] == "https://s.test"
    assert out[1]["title"] == "Big Name"
    assert len(await discovery._feedsearch_feeds("https://s.test/page", limit=1)) == 1
    assert await discovery._feedsearch_feeds("no-scheme-no-host") == []
    assert await discovery._feedsearch_feeds("https://other.test") == []  # HTTP 404


async def test_page_feed_links(net) -> None:
    html = (
        '<html><head><link rel="alternate" type="application/rss+xml" href="/a.xml">'
        '<link rel="alternate" type="application/atom+xml" href="https://x.test/b.xml">'
        '<link rel="alternate" type="application/rss+xml" href="/a.xml">'
        '<link rel="alternate" type="text/html" href="/fr"></head></html>'
    )
    net["pages"]["https://x.test/"] = (200, "text/html; charset=utf-8", html)
    net["pages"]["https://x.test/data"] = (200, "application/json", "{}")
    assert await discovery._page_feed_links("https://x.test/") == [
        "https://x.test/a.xml",
        "https://x.test/b.xml",
    ]
    assert await discovery._page_feed_links("https://x.test/data") == []
    assert await discovery._page_feed_links("https://x.test/missing") == []
    net["boom"].add("https://x.test/")
    assert await discovery._page_feed_links("https://x.test/") == []


# --- _parse_feed_sync ---


def _items(n: int, **kw: str) -> list[dict[str, str]]:
    return [{"title": f"Headline {i}", "link": f"https://site.test/a{i}", **kw} for i in range(n)]


def test_parse_feed_sync_blocked_hosts_and_empty(net) -> None:
    assert discovery._parse_feed_sync("https://www.google.com/rss") is None
    assert discovery._parse_feed_sync("https://www.youtube.com/watch") is None
    assert discovery._parse_feed_sync("https://nothing.test/feed") is None  # nothing parseable


def test_parse_feed_sync_youtube_channel_feed_allowed(net) -> None:
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
    net["feeds"][url] = _rss(
        [{"title": "Vid", "link": "https://youtube.com/watch?v=1"}], title="Chan"
    )
    res = discovery._parse_feed_sync(url, True)
    assert res is not None and res["title"] == "Chan" and res["access_level"] == "paywalled"


def test_parse_feed_sync_access_levels(net) -> None:
    long = "x" * 900
    net["feeds"]["https://full.test/rss"] = _rss(_items(3, desc=long))
    net["feeds"]["https://short.test/rss"] = _rss(_items(3, desc="brief"))
    net["feeds"]["https://marker.test/rss"] = _rss(
        [{"title": "Réservé aux abonnés: news", "link": "https://m.test/1"}]
    )
    net["feeds"]["https://html.test/rss"] = _rss([{"title": "T", "link": "https://wall.test/1"}])
    net["pages"]["https://wall.test/1"] = (200, "text/html", '<div class="paywall">')
    net["pages"]["https://site.test/a0"] = (200, "text/html", "<p>free</p>")

    full = discovery._parse_feed_sync("https://full.test/rss")
    assert full is not None and full["access_level"] == "free_full"
    assert discovery._parse_feed_sync("https://short.test/rss")["access_level"] == "free_excerpt"  # type: ignore[index]
    assert discovery._parse_feed_sync("https://marker.test/rss")["access_level"] == "paywalled"  # type: ignore[index]
    assert discovery._parse_feed_sync("https://html.test/rss")["access_level"] == "paywalled"  # type: ignore[index]


def test_parse_feed_sync_metadata_and_freshness(net) -> None:
    net["feeds"]["https://meta.test/rss"] = _rss(
        _items(2, date="Tue, 02 Jan 2024 10:00:00 GMT", desc="d"), title="", link=""
    )
    res = discovery._parse_feed_sync("https://meta.test/rss")
    assert res is not None
    assert res["title"] == "meta.test" and res["site_url"] == "https://meta.test"
    assert res["sample_titles"] == ["Headline 0", "Headline 1"]
    assert res["sample_articles"][0] == {
        "title": "Headline 0",
        "url": "https://site.test/a0",
        "published_at": "Tue, 02 Jan 2024 10:00:00 GMT",
    }
    assert res["newest_ts"] == 1704189600.0


# --- _probe_url / probe_page_feed ---


async def test_probe_url_direct_and_https_upgrade(net) -> None:
    net["feeds"]["https://direct.test/feed.xml"] = _rss(_items(1))
    res = await discovery._probe_url("http://direct.test/feed.xml")
    assert res is not None and res["url"] == "https://direct.test/feed.xml"


async def test_probe_url_refuses_search_hosts_and_specific_feed_paths(net) -> None:
    assert await discovery._probe_url("https://www.bing.com/news") is None
    net["pages"]["https://site.test/topic/feed"] = (200, "text/html", "<html></html>")
    assert await discovery._probe_url("https://site.test/topic/feed") is None
    assert not any(
        "feedsearch" in c for c in net["calls"]
    )  # no generic fallbacks for explicit feed paths


async def test_probe_url_follows_alternate_link_with_paywall_flag(net) -> None:
    html = (
        '<script type="application/ld+json">{"isAccessibleForFree": false}</script>'
        '<link rel="alternate" type="application/rss+xml" href="/real.xml">'
    )
    net["pages"]["https://page.test/home"] = (200, "text/html", html)
    net["feeds"]["https://page.test/real.xml"] = _rss(_items(2, desc="d"))
    res = await discovery._probe_url("https://page.test/home")
    assert res is not None and res["url"] == "https://page.test/real.xml"
    assert res["access_level"] == "paywalled"


async def test_probe_url_tries_common_paths_then_feedsearch(net) -> None:
    net["pages"]["https://common.test/"] = (200, "text/html", "<html></html>")
    net["feeds"]["https://common.test/rss.xml"] = _rss(_items(1))
    res = await discovery._probe_url("https://common.test/")
    assert res is not None and res["url"] == "https://common.test/rss.xml"

    net["pages"]["https://fs.test/"] = (200, "text/html", "<html></html>")
    net["pages"]["https://feedsearch.dev/api/v1/search?url=fs.test"] = (
        200,
        "application/json",
        '[{"url": "https://fs.test/hidden"}]',
    )
    net["feeds"]["https://fs.test/hidden"] = _rss(_items(1))
    res = await discovery._probe_url("https://fs.test/")
    assert res is not None and res["url"] == "https://fs.test/hidden"

    assert await discovery._probe_url("https://nothing.test/") is None


async def test_probe_page_feed_falls_back_to_home_page_and_times_out(net, monkeypatch) -> None:
    net["pages"]["https://art.test/"] = (
        200,
        "text/html",
        '<link rel="alternate" type="application/rss+xml" href="/f.xml">',
    )
    net["feeds"]["https://art.test/f.xml"] = _rss(_items(1))
    res = await discovery.probe_page_feed("https://art.test/2024/some-article")
    assert res is not None and res["url"] == "https://art.test/f.xml"
    assert await discovery.probe_page_feed("https://none.test/2024/some-article") is None

    async def slow(url: str):
        raise TimeoutError

    monkeypatch.setattr(discovery, "_probe_url", slow)
    assert await discovery.probe_page_feed("https://art.test/x") is None


async def test_probe_page_feeds_youtube_channel_branch(monkeypatch) -> None:
    async def fake_resolve(raw: str):
        return youtube.Channel("UCabcdefghijklmnopqrstuv", "My Channel")

    monkeypatch.setattr(youtube, "resolve_channel", fake_resolve)
    out = await discovery.probe_page_feeds("https://www.youtube.com/@someone")
    assert (
        len(out) == 1 and out[0]["title"] == "My Channel" and out[0]["access_level"] == "free_full"
    )
    assert out[0]["url"].startswith("https://www.youtube.com/feeds/videos.xml")

    async def fail(raw: str):
        raise youtube.YouTubeError(youtube.ErrorCode.UNAVAILABLE)

    monkeypatch.setattr(youtube, "resolve_channel", fail)
    assert await discovery.probe_page_feeds("@someone") == []


async def test_probe_page_feeds_merges_declared_and_feedsearch_feeds(net) -> None:
    net["pages"]["https://multi.test/politik/story"] = (
        200,
        "text/html",
        '<link rel="alternate" type="application/rss+xml" href="/main.xml">',
    )
    net["feeds"]["https://multi.test/main.xml"] = _rss(_items(1), title="Main")
    payload = (
        '[{"url": "https://multi.test/main.xml", "score": 5},'
        ' {"url": "https://multi.test/politik/feed.xml", "score": 1, "title": "Politik"},'
        ' {"url": "https://multi.test/sport/feed.xml", "score": 9, "title": "Sport"}]'
    )
    net["pages"]["https://feedsearch.dev/api/v1/search?url=multi.test&info=true"] = (
        200,
        "application/json",
        payload,
    )
    out = await discovery.probe_page_feeds("https://multi.test/politik/story")
    urls = [f["url"] for f in out]
    assert urls[0] == "https://multi.test/main.xml"  # declared first, not duplicated by feedsearch
    # extras are ordered by path affinity with the page, not by feedsearch score
    assert urls[1:] == ["https://multi.test/politik/feed.xml", "https://multi.test/sport/feed.xml"]


async def test_probe_page_feeds_single_fallback_and_timeout(net, monkeypatch) -> None:
    net["feeds"]["https://solo.test/feed.xml"] = _rss(_items(1))
    out = await discovery.probe_page_feeds("https://solo.test/feed.xml")
    assert [f["url"] for f in out] == ["https://solo.test/feed.xml"]
    assert await discovery.probe_page_feeds("https://empty.test/") == []

    async def slow(page: str):
        raise TimeoutError

    monkeypatch.setattr(discovery, "_page_feed_links", slow)
    assert await discovery.probe_page_feeds("https://solo.test/") == []


# --- LLM / news helpers ---


async def test_translate_term(db_session, monkeypatch) -> None:
    async def chat(system: str, user: str, **kw: Any):
        return {"term": "  economie "}, 2

    async with db_session() as s:
        monkeypatch.setattr(llm_client, "is_configured", lambda: False)
        assert await discovery._translate_term(s, "economy", "fr") == "economy"
        monkeypatch.setattr(llm_client, "is_configured", lambda: True)
        assert await discovery._translate_term(s, "economy", None) == "economy"
        assert await discovery._translate_term(s, "economy", "en") == "economy"
        monkeypatch.setattr(llm_client, "chat_json", chat)
        assert await discovery._translate_term(s, "economy", "fr") == "economie"

        async def blank(system: str, user: str, **kw: Any):
            return {"term": " "}, 1

        monkeypatch.setattr(llm_client, "chat_json", blank)
        assert await discovery._translate_term(s, "economy", "fr") == "economy"

        async def boom(system: str, user: str, **kw: Any):
            raise llm_client.LLMError("down")

        monkeypatch.setattr(llm_client, "chat_json", boom)
        assert await discovery._translate_term(s, "economy", "fr") == "economy"


NEWS_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>n</title>
<item><title>a</title><source url="https://www.paper.test">The Paper</source></item>
<item><title>b</title><source url="https://paper.test/x">Other name</source></item>
<item><title>c</title><source url="https://news.google.com">G</source></item>
<item><title>d</title></item>
<item><title>e</title><source url="https://other.test">Other</source></item>
</channel></rss>"""


def test_parse_news_sources_counts_hosts_and_skips_blocked() -> None:
    assert discovery._parse_news_sources(NEWS_XML) == {
        "paper.test": (2, "The Paper"),
        "other.test": (1, "Other"),
    }


async def test_news_mentions_http_handling(net) -> None:
    assert await discovery._news_mentions("climate", "fr", "FR") == {}  # 404
    net["pages"][discovery.NEWS_URL] = (200, "application/xml", NEWS_XML)
    out = await discovery._news_mentions("climate", "fr", "FR")
    assert out["paper.test"] == (2, "The Paper")
    assert any("hl=fr" in c and "gl=FR" in c and "ceid=FR%3Afr" in c for c in net["calls"])
    net["boom"].add(discovery.NEWS_URL)
    assert await discovery._news_mentions("climate", "fr", "FR") == {}


def test_merge_news_matches_by_site_or_feed_host_and_caps() -> None:
    by_site = discovery._Lead(
        site_url="https://www.paper.test", feed_url="https://cdn.test/f", sources={"directory"}
    )
    by_feed = discovery._Lead(
        site_url="https://elsewhere.test",
        feed_url="https://feeds.other.test/f",
        sources={"directory"},
    )
    mentions = {
        "paper.test": (3, "Paper"),
        "feeds.other.test": (1, "x"),
        "new.test": (2, "New Pub"),
    }
    leads = discovery._merge_news([by_site, by_feed], mentions)
    assert by_site.mentions == 3 and by_site.sources == {"directory", "news"}
    assert by_feed.mentions == 1
    added = [ld for ld in leads if ld.site_url == "https://new.test"]
    assert len(added) == 1 and added[0].title == "New Pub" and added[0].sources == {"news"}

    many = {f"h{i}.test": (i, "n") for i in range(discovery.MAX_NEWS_HOSTS + 10)}
    capped = discovery._merge_news([], many)
    assert len(capped) == discovery.MAX_NEWS_HOSTS
    assert capped[0].mentions == discovery.MAX_NEWS_HOSTS + 9  # best-mentioned first


async def test_suggested_leads_filters_and_records_usage(db_session, monkeypatch) -> None:
    payload = {
        "suggestions": [
            {"name": "Good", "website": "http://good.test", "feed_url": "http://good.test/rss"},
            {"name": "NoFeed", "website": "https://nofeed.test", "feed_url": "ftp://bad"},
            {"name": "Blocked", "website": "https://www.youtube.com/c/x"},
            {"name": "NoSite", "website": ""},
            "junk",
        ]
    }

    async def chat(system: str, user: str, **kw: Any):
        return payload, 4

    monkeypatch.setattr(llm_client, "chat_json", chat)
    async with db_session() as s:
        leads = await discovery._suggested_leads(s, "cooking", "fr", "France")
        await s.commit()
        from sqlalchemy import select

        from app.models import LLMUsage

        kinds = [u.kind for u in await s.scalars(select(LLMUsage))]
    assert [(ld.title, ld.site_url, ld.feed_url) for ld in leads] == [
        ("Good", "https://good.test", "https://good.test/rss"),
        ("NoFeed", "https://nofeed.test", None),
    ]
    assert all(ld.sources == {"ai"} for ld in leads) and kinds == ["discovery_suggest"]

    async def weird(system: str, user: str, **kw: Any):
        return {"suggestions": "nope"}, 1

    monkeypatch.setattr(llm_client, "chat_json", weird)
    async with db_session() as s:
        assert await discovery._suggested_leads(s, "x", None, None) == []

    async def boom(system: str, user: str, **kw: Any):
        raise llm_client.LLMError("down")

    monkeypatch.setattr(llm_client, "chat_json", boom)
    async with db_session() as s:
        assert await discovery._suggested_leads(s, "x", None, None) == []


async def test_covers_topic_only_checks_ai_leads(db_session, monkeypatch) -> None:
    res = {"title": "T", "description": "d", "sample_titles": ["h"]}
    ai = discovery._Lead(site_url="s", sources={"ai"})
    catalog = discovery._Lead(site_url="s", sources={"directory"})
    verdicts: list[Any] = [{"covers": False}]

    async def chat(system: str, user: str, **kw: Any):
        v = verdicts.pop(0)
        if isinstance(v, Exception):
            raise v
        return v, 1

    monkeypatch.setattr(llm_client, "chat_json", chat)
    async with db_session() as s:
        monkeypatch.setattr(llm_client, "is_configured", lambda: False)
        assert await discovery._covers_topic(s, ["t"], res, ai)  # LLM off -> accept
        monkeypatch.setattr(llm_client, "is_configured", lambda: True)
        assert await discovery._covers_topic(s, ["t"], res, catalog)  # no LLM call
        assert await discovery._covers_topic(s, [], res, ai)
        assert not await discovery._covers_topic(s, ["t"], res, ai)  # model says no
        verdicts.append({"covers": True})
        assert await discovery._covers_topic(s, ["t"], res, ai)
        verdicts.append({})  # missing verdict -> accept
        assert await discovery._covers_topic(s, ["t"], res, ai)
        verdicts.append(llm_client.LLMError("down"))  # failure -> accept
        assert await discovery._covers_topic(s, ["t"], res, ai)


async def test_probe_lead_tries_feed_then_site(monkeypatch) -> None:
    tried: list[str] = []

    async def fake_probe(url: str):
        tried.append(url)
        return {"url": url} if url == "https://site.test" else None

    monkeypatch.setattr(discovery, "_probe_url", fake_probe)
    lead = discovery._Lead(site_url="https://site.test", feed_url="https://site.test/rss")
    assert await discovery._probe_lead(lead) == {"url": "https://site.test"}
    assert tried == ["https://site.test/rss", "https://site.test"]

    tried.clear()
    same = discovery._Lead(site_url="https://www.x.test/feed/", feed_url="https://x.test/feed")
    assert await discovery._probe_lead(same) is None
    assert len(tried) == 1  # identical feed/site identity probed once
    assert await discovery._probe_lead(discovery._Lead(site_url="")) is None


async def test_directory_leads_dedupes_and_counts_terms(monkeypatch) -> None:
    from app.services import feed_directory

    item = {"feed_id": "feed/https://a.test/rss", "title": "A", "subscribers": 1}
    other = {"feed_id": "feed/https://b.test/rss", "title": "B"}

    async def topic(name: str, language: str | None):
        return [item] if name == "politics" else [item, other]

    async def search(q: str, language: str | None, count: int):
        return [other]

    monkeypatch.setattr(feed_directory, "topic_feeds", topic)
    monkeypatch.setattr(feed_directory, "search_feeds", search)
    leads, terms = await discovery._directory_leads(["Politics", "World", "free text"], "", "fr")
    by_url = {ld.feed_url: ld for ld in leads}
    assert terms == 3
    assert by_url["https://a.test/rss"].terms_matched == 2 and by_url["https://a.test/rss"].curated
    assert by_url["https://b.test/rss"].terms_matched == 2  # world (curated) + search (not)
    assert by_url["https://b.test/rss"].curated  # curated wins once any batch was curated

    leads, terms = await discovery._directory_leads([], "", None)
    assert leads == [] and terms == 1
    assert time.time() > 0
