"""OPML import tests."""

from httpx import AsyncClient
from sqlalchemy import func, select
from tests.conftest import setup_admin

from app.models import Feed, UserFeed
from app.services.ingest import parse_opml

OPML = b"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Subscriptions</title></head>
  <body>
    <outline text="Tech">
      <outline text="The Verge" title="The Verge" type="rss"
               xmlUrl="https://www.theverge.com/rss/index.xml"
               htmlUrl="https://www.theverge.com/"/>
      <outline text="Ars" title="Ars Technica" type="rss"
               xmlUrl="https://feeds.arstechnica.com/arstechnica/index"/>
    </outline>
    <outline text="No URL - a folder without feeds"/>
    <outline text="Bad" xmlUrl="ftp://not-http.example.com/feed"/>
  </body>
</opml>
"""


def test_parse_opml_extracts_feeds() -> None:
    feeds = parse_opml(OPML)
    assert len(feeds) == 3
    titles = {t for t, _ in feeds}
    assert "The Verge" in titles and "Ars Technica" in titles


async def test_import_opml_endpoint(client: AsyncClient, db_session) -> None:
    await setup_admin(client)

    # Pre-existing feed should be skipped
    async with db_session() as s:
        feed = Feed(url="https://www.theverge.com/rss/index.xml", title="Existing")
        s.add(feed)
        await s.flush()
        s.add(UserFeed(user_id=1, feed_id=feed.id))
        await s.commit()

    r = await client.post(
        "/api/feeds/import-opml",
        files={"file": ("subs.opml", OPML, "text/x-opml")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["added"] == 1  # Ars only; Verge skipped, ftp invalid
    assert body["skipped_existing"] == 1
    assert body["invalid"] == 1

    async with db_session() as s:
        count = await s.scalar(select(func.count(Feed.id)))
        assert count == 2  # pre-existing + Ars

    # Re-import: everything skipped, idempotent
    r = await client.post(
        "/api/feeds/import-opml", files={"file": ("subs.opml", OPML, "text/x-opml")}
    )
    assert r.json()["added"] == 0


async def test_import_opml_rejects_garbage(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post(
        "/api/feeds/import-opml", files={"file": ("x.opml", b"not xml at all", "text/x-opml")}
    )
    assert r.status_code == 400


async def test_export_opml_roundtrip(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    async with db_session() as s:
        f1 = Feed(url="https://www.theverge.com/rss/index.xml", title="The Verge")
        f2 = Feed(url="newsletter:list@example.com", title="A Newsletter", kind="mail")
        s.add_all([f1, f2])
        await s.flush()
        s.add_all([UserFeed(user_id=1, feed_id=f1.id), UserFeed(user_id=1, feed_id=f2.id)])
        await s.commit()

    r = await client.get("/api/feeds/export-opml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-opml")
    body = r.text
    assert "The Verge" in body
    assert "theverge.com" in body
    assert "newsletter:" not in body  # mail feeds excluded — pseudo-URL isn't a real feed

    # Round-trip through parse_opml to confirm it's valid, re-importable OPML
    parsed = parse_opml(body.encode())
    assert parsed == [("The Verge", "https://www.theverge.com/rss/index.xml")]
