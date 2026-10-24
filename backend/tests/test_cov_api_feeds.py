"""Feeds API: subscriptions across users, YouTube resolution, discovery, probe, OPML/CSV import."""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from tests.conftest import setup_admin

from app.api import feeds as feeds_api
from app.models import Article, Feed, UserFeed
from app.services import youtube

READER = {"username": "reader", "password": "readerpass1"}
URL = "https://shared.example.com/rss"


async def _login_as_reader(client: AsyncClient) -> None:
    r = await client.post("/api/users", json=READER)
    assert r.status_code == 201
    await client.post("/api/auth/logout")
    assert (await client.post("/api/auth/login", json=READER)).status_code == 200


async def _login_as_admin(client: AsyncClient) -> None:
    await client.post("/api/auth/logout")
    r = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "supersecret1"}
    )
    assert r.status_code == 200


async def test_second_user_subscribing_to_existing_feed_shares_it(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    admin_feed = (await client.post("/api/feeds", json={"url": URL, "title": "Shared"})).json()
    async with db_session() as s:
        s.add(Article(feed_id=admin_feed["id"], guid="g", url="https://n/1", title="t"))
        await s.commit()

    await _login_as_reader(client)
    assert (await client.get("/api/feeds")).json() == []
    r = await client.post("/api/feeds", json={"url": URL})
    assert r.status_code == 201
    assert r.json()["id"] == admin_feed["id"]  # same global feed, no duplicate
    assert (await client.post("/api/feeds", json={"url": URL})).status_code == 409
    assert len((await client.get("/api/feeds")).json()) == 1

    async with db_session() as s:
        assert await s.scalar(select(func.count(Feed.id))) == 1
        assert await s.scalar(select(func.count(UserFeed.feed_id))) == 2


async def test_non_subscriber_cannot_touch_feed(client: AsyncClient) -> None:
    await setup_admin(client)
    feed_id = (await client.post("/api/feeds", json={"url": URL, "title": "A"})).json()["id"]
    await _login_as_reader(client)

    assert (await client.patch(f"/api/feeds/{feed_id}", json={"title": "x"})).status_code == 404
    assert (await client.delete(f"/api/feeds/{feed_id}")).status_code == 404
    assert (await client.post(f"/api/feeds/{feed_id}/refresh")).status_code == 404


async def test_delete_only_unsubscribes_while_others_remain(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    feed_id = (await client.post("/api/feeds", json={"url": URL, "title": "A"})).json()["id"]
    await _login_as_reader(client)
    await client.post("/api/feeds", json={"url": URL})

    assert (await client.delete(f"/api/feeds/{feed_id}")).status_code == 204
    assert (await client.get("/api/feeds")).json() == []

    await _login_as_admin(client)
    assert [f["id"] for f in (await client.get("/api/feeds")).json()] == [feed_id]
    async with db_session() as s:
        assert await s.get(Feed, feed_id) is not None


async def test_refresh_newsletter_feed_rejected_and_refresh_all_is_scoped(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    polled: list[int] = []

    async def fake_poll(session: Any, feed: Feed) -> int:
        polled.append(feed.id)
        return 3

    monkeypatch.setattr(feeds_api, "poll_feed", fake_poll)
    async with db_session() as s:
        mail = Feed(url="newsletter:a@b.c", title="Mail", kind="mail")
        rss = Feed(url="https://r.example.com/rss", title="R")
        other = Feed(url="https://other.example.com/rss", title="Other")
        off = Feed(url="https://off.example.com/rss", title="Off", is_enabled=False)
        s.add_all([mail, rss, other, off])
        await s.flush()
        for f in (mail, rss, off):  # `other` is not subscribed by admin
            s.add(UserFeed(user_id=1, feed_id=f.id))
        await s.commit()
        mail_id, rss_id = mail.id, rss.id

    r = await client.post(f"/api/feeds/{mail_id}/refresh")
    assert r.status_code == 400
    assert "Newsletter" in r.json()["detail"]

    r = await client.post("/api/feeds/refresh")
    assert r.json() == {"feeds_polled": 1, "new_articles": 3}
    assert polled == [rss_id]


async def test_create_feed_title_lookup_and_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(feeds_api, "_fetch_feed_title", lambda url: "Fetched title")
    r = await client.post("/api/feeds", json={"url": "https://t1.example.com/rss"})
    assert r.json()["title"] == "Fetched title"

    def boom(url: str) -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(feeds_api, "_fetch_feed_title", boom)
    r = await client.post("/api/feeds", json={"url": "https://t2.example.com/rss"})
    assert r.status_code == 201
    assert r.json()["title"] == ""


async def test_create_feed_youtube_resolution(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    channel_feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UC" + "a" * 22

    async def ok(raw: str) -> Any:
        return type("C", (), {"feed_url": channel_feed})()

    monkeypatch.setattr(youtube, "resolve_channel", ok)
    r = await client.post("/api/feeds", json={"url": "@somechannel", "title": "YT"})
    assert r.status_code == 201
    assert r.json()["url"] == channel_feed

    def failing(code: youtube.ErrorCode) -> Any:
        async def _raise(raw: str) -> Any:
            raise youtube.YouTubeError(code)

        return _raise

    monkeypatch.setattr(youtube, "resolve_channel", failing(youtube.ErrorCode.UNAVAILABLE))
    r = await client.post("/api/feeds", json={"url": "@gone"})
    assert r.status_code == 422
    assert "unavailable" in r.json()["detail"]

    monkeypatch.setattr(youtube, "resolve_channel", failing(youtube.ErrorCode.NETWORK))
    assert (await client.post("/api/feeds", json={"url": "@down"})).status_code == 502


async def test_discover_excludes_followed_urls(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    await client.post("/api/feeds", json={"url": URL, "title": "Followed"})
    seen: dict[str, Any] = {}

    async def fake_discover(session: Any, **kw: Any) -> list[dict[str, Any]]:
        seen.update(kw)
        return [{"title": "New", "url": "https://new.example.com/rss"}]

    monkeypatch.setattr(feeds_api.discovery, "discover_feeds", fake_discover)
    r = await client.post(
        "/api/feeds/discover", json={"query": "ai", "excluded_urls": ["https://x/y"]}
    )
    assert r.status_code == 200
    assert [f["url"] for f in r.json()["feeds"]] == ["https://new.example.com/rss"]
    assert seen["excluded_urls"] == ["https://x/y", URL]
    assert seen["query"] == "ai"


async def _ndjson(client: AsyncClient, body: dict[str, Any]) -> list[dict[str, Any]]:
    r = await client.post("/api/feeds/discover/stream", json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in r.text.splitlines() if line]


async def test_discover_stream_emits_events_then_done(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def fake_discover(session: Any, *, emit: Any, **kw: Any) -> list[dict[str, Any]]:
        await emit({"type": "stage", "stage": "search"})
        return [{"title": "F", "url": "https://f.example.com/rss"}]

    monkeypatch.setattr(feeds_api.discovery, "discover_feeds", fake_discover)
    events = await _ndjson(client, {"query": "x"})
    assert events[0] == {"type": "stage", "stage": "search"}
    assert events[-1]["type"] == "done"
    assert events[-1]["feeds"][0]["url"] == "https://f.example.com/rss"


async def test_discover_stream_reports_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)

    async def broken(session: Any, **kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("secret internals")

    monkeypatch.setattr(feeds_api.discovery, "discover_feeds", broken)
    events = await _ndjson(client, {"query": "x"})
    assert events == [{"type": "error", "message": "Discovery failed"}]


async def test_probe_validation_and_results(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    await client.post("/api/feeds", json={"url": "https://site.example.com/feed", "title": "S"})

    assert (await client.post("/api/feeds/probe", json={"url": "ftp://x"})).status_code == 422

    async def none_found(url: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(feeds_api.discovery, "probe_page_feeds", none_found)
    r = await client.post("/api/feeds/probe", json={"url": "https://site.example.com/"})
    assert r.json() == {"feed": None, "already_followed": False, "feeds": []}

    async def found(url: str) -> list[dict[str, Any]]:
        return [
            {"title": "Mine", "url": "http://www.site.example.com/feed/", "site_url": None,
             "description": "d"},
            {"title": "New", "url": "https://site.example.com/other", "site_url": None,
             "description": "", "access_level": "paywalled"},
        ]

    monkeypatch.setattr(feeds_api.discovery, "probe_page_feeds", found)
    r = await client.post("/api/feeds/probe", json={"url": "https://site.example.com/"})
    body = r.json()
    # feed_key ignores scheme/www/trailing slash → recognized as already followed
    assert body["already_followed"] is True
    assert body["feed"]["title"] == "Mine"
    assert [f["already_followed"] for f in body["feeds"]] == [True, False]
    assert body["feeds"][1]["access_level"] == "paywalled"
    assert "already_followed" not in body["feed"]


OPML = b"""<?xml version="1.0"?><opml version="2.0"><body>
<outline text="One" xmlUrl="https://one.example.com/rss"/>
<outline text="Dup" xmlUrl="https://one.example.com/rss"/>
<outline text="Bad" xmlUrl="ftp://bad.example.com/rss"/>
<outline text="Two" xmlUrl="https://two.example.com/rss"/>
</body></opml>"""


async def test_import_and_export_opml(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    r = await client.post("/api/feeds/import-opml", files={"file": ("s.opml", OPML)})
    assert r.status_code == 201
    body = r.json()
    assert (body["added"], body["skipped_existing"], body["invalid"]) == (2, 1, 1)
    assert [f["title"] for f in body["feeds"]] == ["One", "Two"]

    # Re-import: everything already followed
    r = await client.post("/api/feeds/import-opml", files={"file": ("s.opml", OPML)})
    assert r.json()["added"] == 0
    assert r.json()["skipped_existing"] == 3

    r = await client.get("/api/feeds/export-opml")
    assert r.headers["content-type"].startswith("text/x-opml")
    assert "newsgator-subscriptions.opml" in r.headers["content-disposition"]
    assert "https://one.example.com/rss" in r.text
    assert "https://two.example.com/rss" in r.text

    # A second user importing known feeds subscribes without duplicating them
    await _login_as_reader(client)
    r = await client.post("/api/feeds/import-opml", files={"file": ("s.opml", OPML)})
    assert r.json()["added"] == 2
    async with db_session() as s:
        assert await s.scalar(select(func.count(Feed.id))) == 2


async def test_import_opml_rejects_garbage(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post("/api/feeds/import-opml", files={"file": ("x", b"not xml <<<")})
    assert r.status_code == 400


async def test_import_subscriptions_csv_opml_and_invalid(client: AsyncClient) -> None:
    await setup_admin(client)
    csv_bytes = (
        "Channel ID,Channel URL,Channel title\n"
        f"UC{'a' * 22},http://www.youtube.com/channel/UC{'a' * 22},Chan A\n"
        "not-an-id,http://www.youtube.com/channel/zzz,Broken\n"
    ).encode()
    r = await client.post(
        "/api/feeds/import-subscriptions", files={"file": ("subscriptions.csv", csv_bytes)}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["added"], body["invalid"]) == (1, 1)
    assert body["feeds"][0]["url"].endswith("channel_id=UC" + "a" * 22)

    r = await client.post(
        "/api/feeds/import-subscriptions", files={"file": ("s.opml", OPML)}
    )
    assert r.json()["added"] == 2
    assert r.json()["invalid"] == 1

    r = await client.post(
        "/api/feeds/import-subscriptions", files={"file": ("bad.xml", b"<opml><unclosed")}
    )
    assert r.status_code == 400
