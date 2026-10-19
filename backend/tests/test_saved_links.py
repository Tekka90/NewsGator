from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.models import Article, Feed, UserFeed
from app.services import discovery


async def test_import_url_creates_article_in_saved_feed(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    r = await client.post(
        "/api/stories/import-url", json={"url": "https://news.example.com/a?utm_source=x"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["already_known"] is False

    async with db_session() as s:
        article = await s.get(Article, body["article_id"])
        feed = await s.get(Feed, body["feed_id"])
        assert article is not None and feed is not None
        assert article.url == "https://news.example.com/a"
        assert article.processing_state == "fetched"
        assert feed.kind == "saved" and feed.url == "saved:1"
        assert await s.scalar(
            select(UserFeed).where(UserFeed.user_id == 1, UserFeed.feed_id == feed.id)
        )

    again = await client.post("/api/stories/import-url", json={"url": "https://news.example.com/a"})
    assert again.status_code == 201
    assert again.json()["already_known"] is True
    assert again.json()["article_id"] == body["article_id"]


async def test_import_url_rejects_non_http(client: AsyncClient) -> None:
    await setup_admin(client)
    for bad in ("ftp://x.example.com/a", "javascript:alert(1)", "not a url", "https://"):
        r = await client.post("/api/stories/import-url", json={"url": bad})
        assert r.status_code == 422, bad


async def test_probe_page_feed(client: AsyncClient, monkeypatch) -> None:
    await setup_admin(client)

    async def fake(url: str):
        return [
            {
                "url": "https://news.example.com/feed.xml",
                "title": "Example News",
                "site_url": "https://news.example.com",
                "description": "",
            },
            {
                "url": "https://news.example.com/politics.xml",
                "title": "Politics",
                "site_url": "https://news.example.com",
                "description": "",
            },
        ]

    monkeypatch.setattr(discovery, "probe_page_feeds", fake)
    r = await client.post("/api/feeds/probe", json={"url": "https://news.example.com/a"})
    assert r.status_code == 200
    assert r.json()["feed"]["url"] == "https://news.example.com/feed.xml"
    assert r.json()["already_followed"] is False
    assert [f["title"] for f in r.json()["feeds"]] == ["Example News", "Politics"]

    await client.post("/api/feeds", json={"url": "https://news.example.com/feed.xml", "title": "x"})
    again = await client.post("/api/feeds/probe", json={"url": "https://news.example.com/a"})
    assert again.json()["already_followed"] is True
    assert [f["already_followed"] for f in again.json()["feeds"]] == [True, False]


async def test_probe_without_feed(client: AsyncClient, monkeypatch) -> None:
    await setup_admin(client)

    async def none(url: str):
        return []

    monkeypatch.setattr(discovery, "probe_page_feeds", none)
    r = await client.post("/api/feeds/probe", json={"url": "https://news.example.com/a"})
    assert r.json() == {"feed": None, "already_followed": False, "feeds": []}
