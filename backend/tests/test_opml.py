"""OPML import tests."""

from httpx import AsyncClient
from sqlalchemy import func, select
from tests.conftest import setup_admin

from app.models import Feed
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
        s.add(Feed(url="https://www.theverge.com/rss/index.xml", title="Existing"))
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
