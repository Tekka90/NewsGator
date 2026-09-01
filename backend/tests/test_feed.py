"""Story RSS feed tests: auth, content, filters, guid/pubDate semantics."""

from datetime import UTC, datetime
from xml.etree.ElementTree import fromstring

from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.models import Article, Category, Feed, Story, StoryRevision, StoryState, User

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"


async def _make_stories(db_session) -> tuple[int, int]:
    """Two stories with articles; story 1 also has a revision. Returns ids."""
    async with db_session() as s:
        s.add(Category(name="tech"))
        s.add(Category(name="world"))
        feed = Feed(url="https://f.example.com/rss", title="Feed")
        s.add(feed)
        await s.flush()
        s1 = Story(
            title="Big Event",
            summary="Line one.\n\nLine two.",
            category="tech",
            image_url="https://news.example.com/lead.jpg",
            version=2,
        )
        s2 = Story(title="Other News", summary="Short.", category="world")
        s.add(s1)
        s.add(s2)
        await s.flush()
        s.add(
            Article(
                feed_id=feed.id,
                guid="a1",
                url="https://news.example.com/one",
                title="Article One",
                story_id=s1.id,
                published_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
                processing_state="clustered",
            )
        )
        s.add(
            Article(
                feed_id=feed.id,
                guid="a2",
                url="https://other.example.com/two",
                title="Article Two",
                story_id=s1.id,
                published_at=datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
                processing_state="clustered",
            )
        )
        s.add(
            Article(
                feed_id=feed.id,
                guid="a3",
                url="https://third.example.com/three",
                title="Article Three",
                story_id=s2.id,
                processing_state="clustered",
            )
        )
        s.add(
            StoryRevision(
                story_id=s1.id,
                version=2,
                summary="Line one.\n\nLine two.",
                created_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
            )
        )
        await s.commit()
        return s1.id, s2.id


async def _token(client: AsyncClient) -> str:
    """Create the admin and return a portable session token (RSS readers can't
    persist cookies — the feed is consumed via ?token=)."""
    await setup_admin(client)
    r = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "supersecret1"}
    )
    assert r.status_code == 200
    client.cookies.clear()
    return r.json()["token"]


def _items(xml: str):
    return fromstring(xml).find("channel").findall("item")


async def test_feed_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/api/feed.xml")
    assert r.status_code == 401


async def test_feed_renders_stories(client: AsyncClient, db_session) -> None:
    s1_id, s2_id = await _make_stories(db_session)
    token = await _token(client)
    r = await client.get(f"/api/feed.xml?token={token}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")

    root = fromstring(r.text)
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("title") == "NewsGator"
    assert channel.find(f"{ATOM}link").get("rel") == "self"

    items = _items(r.text)
    assert len(items) == 2
    by_guid = {i.findtext("guid"): i for i in items}
    assert set(by_guid) == {f"story:{s1_id}", f"story:{s2_id}"}
    assert items[0].find("guid").get("isPermaLink") == "false"

    item1 = by_guid[f"story:{s1_id}"]
    assert item1.findtext("title") == "Big Event"
    # primary link = earliest published article
    assert item1.findtext("link") == "https://other.example.com/two"
    assert item1.findtext("category") == "tech"
    assert "<p>Line one.</p><p>Line two.</p>" in (item1.findtext("description") or "")
    # lead image exposed as media:content
    assert item1.find(f"{MEDIA}content").get("url") == (
        "https://news.example.com/lead.jpg"
    )
    # pubDate is the original publication date; atom:updated tracks the
    # latest revision date (version bump) instead of last_updated_at
    assert "30 Aug 2026" in (item1.findtext("pubDate") or "")
    assert (item1.findtext(f"{ATOM}updated") or "").startswith("2026-08-31")
    # story 2 has no published article dates → pubDate falls back to the
    # revision/last_updated_at date
    assert by_guid[f"story:{s2_id}"].findtext("pubDate")


async def test_feed_filters(client: AsyncClient, db_session) -> None:
    s1_id, s2_id = await _make_stories(db_session)
    token = await _token(client)

    r = await client.get(f"/api/feed.xml?token={token}&category=world")
    items = _items(r.text)
    assert [i.findtext("guid") for i in items] == [f"story:{s2_id}"]
    assert "world" in (fromstring(r.text).find("channel").findtext("title") or "")

    # mark story 1 read → unread=1 must exclude it
    async with db_session() as s:
        user = await s.scalar(select(User))
        s.add(
            StoryState(
                user_id=user.id, story_id=s1_id, is_read=True, read_at_version=2
            )
        )
        await s.commit()
    r = await client.get(f"/api/feed.xml?token={token}&unread=1")
    assert [i.findtext("guid") for i in _items(r.text)] == [f"story:{s2_id}"]


async def test_feed_limit_validated(client: AsyncClient) -> None:
    token = await _token(client)
    r = await client.get(f"/api/feed.xml?token={token}&limit=0")
    assert r.status_code == 422
