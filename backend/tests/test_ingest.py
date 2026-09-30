"""Ingestion + full-text chain tests (Milestone 2)."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ActivityEvent, Article, Feed
from app.services import fulltext, ingest


def _rfc822(dt: datetime) -> str:
    return format_datetime(dt.astimezone(UTC))


# Fixture dates are relative to now — hardcoded dates age out of the first-poll
# backfill window (default 7 days) and silently shrink the fixtures.
_FRESH = _rfc822(datetime.now(UTC) - timedelta(hours=1))

RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<item>
  <title>Apple announces iPhone 18</title>
  <link>https://news.example.com/iphone18?utm_source=rss&amp;utm_medium=feed</link>
  <guid>item-1</guid>
  <pubDate>{_FRESH}</pubDate>
  <description>Short excerpt about the iPhone 18 launch.</description>
</item>
<item>
  <title>Other news</title>
  <link>https://news.example.com/other</link>
  <guid>item-2</guid>
</item>
</channel></rss>
""".encode()

LONG_TEXT = "Long extracted article body. " * 30  # > fulltext_min_chars (400)


def _ok_http(content: bytes = RSS):
    async def fake(feed: Feed) -> tuple[int, bytes, dict[str, str]]:
        return 200, content, {"etag": "v1"}

    return fake


async def _make_feed(factory: async_sessionmaker[AsyncSession], **kwargs) -> Feed:
    kwargs.setdefault("url", "https://news.example.com/rss")
    async with factory() as s:
        feed = Feed(**kwargs)
        s.add(feed)
        await s.commit()
        return feed


async def test_canonicalize_strips_tracking() -> None:
    url = "https://News.example.com/a/b?x=1&utm_source=rss&fbclid=zz#frag"
    assert ingest.canonicalize_url(url) == "https://news.example.com/a/b?x=1"


async def test_poll_creates_articles_and_dedupes(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest, "_http_get", _ok_http())
    # Disable fulltext to isolate ingestion
    feed = await _make_feed(db_session, fetch_fulltext=False)

    async with db_session() as s:
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 2

        # Second poll with same content → dedupe by (feed_id, guid)
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 0

        count = await s.scalar(select(func.count(Article.id)))
        assert count == 2

        # Canonical URL stored without utm params
        a1 = await s.scalar(select(Article).where(Article.guid == "item-1"))
        assert a1 is not None
        assert a1.url == "https://news.example.com/iphone18"
        assert a1.published_at is not None
        assert a1.processing_state == "fulltext"  # fulltext skipped → state advanced

        # Feed title learned from the feed itself
        f = await s.get(Feed, feed.id)
        assert f is not None and f.title == "Test Feed"
        assert f.etag == "v1"
        assert f.empty_polls == 1  # second poll produced nothing new

        # Activity events emitted (SPEC invariant 6)
        actions = (
            await s.scalars(select(ActivityEvent.action).where(ActivityEvent.component == "ingest"))
        ).all()
        assert "feed_poll_start" in actions and "feed_poll_done" in actions


async def test_poll_extracts_article_image(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """image_url comes from the RSS entry: media:content / thumbnail / image
    enclosure, else the first meaningful <img> in the entry's own HTML."""
    rss = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<title>Img Feed</title>
<item>
  <title>With media content</title>
  <link>https://news.example.com/i1</link>
  <guid>img-1</guid>
  <media:content url="https://img.example.com/a.jpg" medium="image" />
</item>
<item>
  <title>With thumbnail</title>
  <link>https://news.example.com/i2</link>
  <guid>img-2</guid>
  <media:thumbnail url="https://img.example.com/b.jpg" />
</item>
<item>
  <title>With enclosure</title>
  <link>https://news.example.com/i3</link>
  <guid>img-3</guid>
  <enclosure url="https://img.example.com/c.jpg" type="image/jpeg" length="0" />
</item>
<item>
  <title>No image</title>
  <link>https://news.example.com/i4</link>
  <guid>img-4</guid>
</item>
<item>
  <title>Inline image in content</title>
  <link>https://news.example.com/i5</link>
  <guid>img-5</guid>
  <content:encoded><![CDATA[<p><img width="1200" height="675"
    src="https://img.example.com/inline.jpg" alt="lead" /> body</p>]]></content:encoded>
</item>
<item>
  <title>Pixel and emoji before real image</title>
  <link>https://news.example.com/i6</link>
  <guid>img-6</guid>
  <description><![CDATA[<img src="https://img.example.com/track.gif" width="1" height="1" />
    <img src="https://img.example.com/emoji/1f600.png" />
    <img src="/relative/d.jpg" />]]></description>
</item>
<item>
  <title>Lazy-loaded image (placeholder src, real URL in data-src)</title>
  <link>https://news.example.com/i7</link>
  <guid>img-7</guid>
  <content:encoded><![CDATA[<p><img
    src="https://news.example.com/themes/x/images/placeholder.svg"
    data-src="https://img.example.com/lazy.jpg"
    class="lazy" alt="lead" /></p>]]></content:encoded>
</item>
</channel></rss>
"""
    monkeypatch.setattr(ingest, "_http_get", _ok_http(rss))
    feed = await _make_feed(db_session, fetch_fulltext=False)

    async with db_session() as s:
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 7
        by_guid = {
            a.guid: a.image_url
            for a in (await s.scalars(select(Article))).all()
        }
    assert by_guid["img-1"] == "https://img.example.com/a.jpg"
    assert by_guid["img-2"] == "https://img.example.com/b.jpg"
    assert by_guid["img-3"] == "https://img.example.com/c.jpg"
    assert by_guid["img-4"] is None
    assert by_guid["img-5"] == "https://img.example.com/inline.jpg"
    # tracking pixel + emoji skipped; relative src resolved against the link
    assert by_guid["img-6"] == "https://news.example.com/relative/d.jpg"
    # lazy-load placeholder src is not a real image → None, so the fulltext
    # stage can recover the page's og:image instead of storing the placeholder
    assert by_guid["img-7"] is None


async def test_poll_cross_feed_url_dedupe(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_http_get", _ok_http())
    feed_a = await _make_feed(db_session, fetch_fulltext=False)
    # Second feed serving the same article URLs (with different tracking params)
    feed_b = await _make_feed(db_session, url="https://other.example.com/rss", fetch_fulltext=False)

    async with db_session() as s:
        assert await ingest.poll_feed(s, await s.get(Feed, feed_a.id)) == 2
        assert await ingest.poll_feed(s, await s.get(Feed, feed_b.id)) == 0  # URL-level dedupe
        count = await s.scalar(select(func.count(Article.id)))
        assert count == 2


async def test_poll_304_not_modified(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_http_get", _ok_http())
    feed = await _make_feed(db_session, fetch_fulltext=False)

    async def not_modified(feed: Feed) -> tuple[int, bytes, dict[str, str]]:
        return 304, b"", {}

    async with db_session() as s:
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 2
        monkeypatch.setattr(ingest, "_http_get", not_modified)
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 0
        f = await s.get(Feed, feed.id)
        assert f is not None and f.consecutive_failures == 0


RSS_MIXED_AGES = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Backfill Feed</title>
<item>
  <title>Fresh news</title>
  <link>https://bf.example.com/fresh</link>
  <guid>fresh-1</guid>
  <pubDate>{_FRESH}</pubDate>
</item>
<item>
  <title>Ancient news</title>
  <link>https://bf.example.com/ancient</link>
  <guid>old-1</guid>
  <pubDate>Mon, 24 Aug 2020 08:00:00 GMT</pubDate>
</item>
<item>
  <title>Undated news</title>
  <link>https://bf.example.com/undated</link>
  <guid>nodate-1</guid>
</item>
</channel></rss>
""".encode()


async def test_first_poll_backfill_window(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """First poll skips entries older than the feed's backfill window; undated
    entries are kept; the window never applies again after the first poll."""
    monkeypatch.setattr(ingest, "_http_get", _ok_http(RSS_MIXED_AGES))
    feed = await _make_feed(db_session, fetch_fulltext=False, backfill_days=7)

    async with db_session() as s:
        # First poll: 2020 entry skipped, fresh + undated kept
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 2
        guids = set((await s.scalars(select(Article.guid))).all())
        assert guids == {"fresh-1", "nodate-1"}

        # Activity event logged the skip (invariant 6)
        actions = (
            await s.scalars(select(ActivityEvent.action).where(ActivityEvent.component == "ingest"))
        ).all()
        assert "backfill_skipped" in actions

        # Second poll re-serving the same old entry: dedupe sees it as new GUID
        # but the window no longer applies → ingested now (window is first-poll only).
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 1
        guids = set((await s.scalars(select(Article.guid))).all())
        assert guids == {"fresh-1", "nodate-1", "old-1"}


async def test_backfill_window_zero_imports_everything(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest, "_http_get", _ok_http(RSS_MIXED_AGES))
    feed = await _make_feed(db_session, fetch_fulltext=False, backfill_days=0)

    async with db_session() as s:
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 3


async def test_backfill_window_default_from_settings(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backfill_days=None follows settings.feed_backfill_days."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "feed_backfill_days", 30)
    monkeypatch.setattr(ingest, "_http_get", _ok_http(RSS_MIXED_AGES))
    feed = await _make_feed(db_session, fetch_fulltext=False)  # backfill_days=None

    async with db_session() as s:
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 2


async def test_failure_policy_backoff_and_disable(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(feed: Feed) -> tuple[int, bytes, dict[str, str]]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ingest, "_http_get", boom)
    feed = await _make_feed(db_session, fetch_fulltext=False)

    async with db_session() as s:
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 0
        f = await s.get(Feed, feed.id)
        assert f is not None
        assert f.consecutive_failures == 1
        assert f.last_error is not None and "connection refused" in f.last_error
        assert f.is_enabled is True

        # Simulate 8 days of continuous failures → auto-disable (SPEC §9)
        f.first_failure_at = datetime.now(UTC) - timedelta(days=8)
        await s.commit()
        await ingest.poll_feed(s, f)
        assert f.is_enabled is False

        actions = (
            await s.scalars(select(ActivityEvent.action).where(ActivityEvent.level == "error"))
        ).all()
        assert "feed_poll_error" in actions and "feed_disabled" in actions


async def test_effective_interval_adaptive() -> None:
    feed = Feed(url="https://x", poll_interval_min=30)
    assert ingest.effective_interval_min(feed) == 30
    feed.empty_polls = 1
    assert ingest.effective_interval_min(feed) == 60  # capped at max default 60
    feed.empty_polls = 5
    assert ingest.effective_interval_min(feed) == 60
    feed.empty_polls = 0
    feed.consecutive_failures = 2
    assert ingest.effective_interval_min(feed) == 120  # 30 * 2^2
    feed.consecutive_failures = 20
    assert ingest.effective_interval_min(feed) == 720  # 12h cap


async def test_is_due() -> None:
    feed = Feed(url="https://x", poll_interval_min=30, is_enabled=True)
    now = datetime.now(UTC)
    assert ingest.is_due(feed, now) is True  # never fetched
    feed.last_fetched_at = now
    assert ingest.is_due(feed, now) is False
    feed.last_fetched_at = now - timedelta(minutes=31)
    assert ingest.is_due(feed, now) is True
    feed.is_enabled = False
    assert ingest.is_due(feed, now) is False


async def test_datetimes_roundtrip_tz_aware(db_session) -> None:
    """Regression: SQLite drops tzinfo; UTCDateTime must re-attach UTC on read.

    Without it, is_due() crashes comparing naive DB values with aware now.
    """
    feed = await _make_feed(db_session, last_fetched_at=datetime.now(UTC))
    async with db_session() as s:
        loaded = await s.scalar(select(Feed).where(Feed.id == feed.id))
        assert loaded is not None
        assert loaded.last_fetched_at is not None
        assert loaded.last_fetched_at.tzinfo is not None
        assert loaded.created_at.tzinfo is not None
        # The scheduler's exact comparison must not raise:
        assert ingest.is_due(loaded, datetime.now(UTC)) is False


# --- full-text chain ---


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    fulltext._archive_failures.clear()
    fulltext._domain_last_fetch.clear()
    fulltext._robots_cache.clear()


def _patch_extract(monkeypatch: pytest.MonkeyPatch, text: str | None) -> None:
    async def fake_extract(html: str) -> str | None:
        return text

    monkeypatch.setattr(fulltext, "_extract_text", lambda html: text)


def _patch_pages(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str | None]) -> None:
    async def fake_fetch(url: str, cookies: dict[str, str] | None = None) -> str | None:
        for prefix, content in pages.items():
            if url.startswith(prefix):
                return content
        return None

    monkeypatch.setattr(fulltext, "_fetch_page", fake_fetch)


async def _article(
    factory: async_sessionmaker[AsyncSession], **feed_kwargs
) -> tuple[Feed, Article]:
    feed = await _make_feed(factory, **feed_kwargs)
    async with factory() as s:
        article = Article(
            feed_id=feed.id,
            guid="g1",
            url="https://news.example.com/story",
            title="Story",
            raw_content="rss excerpt",
        )
        s.add(article)
        await s.commit()
        return feed, article


async def _run_fulltext(s: AsyncSession, feed: Feed, article: Article) -> None:
    f, a = await s.get(Feed, feed.id), await s.get(Article, article.id)
    assert f is not None and a is not None
    await fulltext.fetch_full_text(s, a, f)


def test_extract_text_strips_xml_invalid_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    # Newsletter-linked pages can carry NULL bytes / C0 control chars; they are
    # invalid XML and crash readability's lxml cleaner ("All strings must be
    # XML compatible"). They must be stripped before the readability fallback.
    import trafilatura

    para = "Long newsletter paragraph with plenty of readable prose. " * 10
    dirty = (
        f"<html><body><article><p>{para}\x00\x07</p><p>{para}</p></article></body></html>"
    )
    calls = 0

    def flaky_extract(html: str, *args: object, **kwargs: object) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None  # force the readability fallback
        return "readable fallback text"

    monkeypatch.setattr(trafilatura, "extract", flaky_extract)
    text = fulltext._extract_text(dirty)
    assert text == "readable fallback text"


async def test_fulltext_direct_success(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_extract(monkeypatch, LONG_TEXT)
    _patch_pages(monkeypatch, {"https://news.example.com": "<html>full</html>"})
    feed, article = await _article(db_session)

    async with db_session() as s:
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.full_text == LONG_TEXT
        assert a.content_status == "full"
        assert a.processing_state == "fulltext"

        detail = await s.scalar(
            select(ActivityEvent.detail).where(ActivityEvent.action == "fulltext_fetch")
        )
        assert detail is not None and '"path": "direct"' in detail


async def test_fulltext_archive_fallback(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct returns paywalled/too-short text; archive.is serves the full copy
    monkeypatch.setattr(
        fulltext, "_extract_text", lambda html: "short" if html == "pw" else LONG_TEXT
    )
    _patch_pages(
        monkeypatch,
        {"https://news.example.com": "pw", "https://archive.is": "<html>archived</html>"},
    )
    feed, article = await _article(db_session)

    async with db_session() as s:
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.content_status == "full"
        detail = await s.scalar(
            select(ActivityEvent.detail).where(ActivityEvent.action == "fulltext_fetch")
        )
        assert detail is not None and '"path": "archive.is"' in detail


async def test_fulltext_partial_fallback(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_extract(monkeypatch, None)
    _patch_pages(monkeypatch, {})  # everything fails
    feed, article = await _article(db_session)

    async with db_session() as s:
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.content_status == "partial"
        assert a.content_warning is not None and "robots.txt" in a.content_warning
        assert a.full_text == "rss excerpt"


async def test_archive_failure_cached(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_extract(monkeypatch, None)
    calls: list[str] = []

    async def counting_fetch(url: str, cookies: dict[str, str] | None = None) -> str | None:
        calls.append(url)
        return None

    monkeypatch.setattr(fulltext, "_fetch_page", counting_fetch)
    feed, article = await _article(db_session)

    async with db_session() as s:
        f, a = await s.get(Feed, feed.id), await s.get(Article, article.id)
        assert f is not None and a is not None
        await fulltext.fetch_full_text(s, a, f)
        a.content_status = "full"  # reset outcome, retry
        await fulltext.fetch_full_text(s, a, f)

    archive_calls = [c for c in calls if c.startswith("https://archive.is")]
    assert len(archive_calls) == 1  # second call served from failure cache


async def test_paywall_marker_detection() -> None:
    assert fulltext._looks_paywalled("Please subscribe to continue reading this") is True
    assert fulltext._looks_paywalled(LONG_TEXT) is False


# --- og:image recovery ---

OG_HTML = (
    '<html><head><meta property="og:image" content="https://img.example.com/og.jpg">'
    "</head><body>article</body></html>"
)


async def test_fulltext_recovers_og_image(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed had no media enclosure → og:image from the page becomes article.image_url."""
    _patch_extract(monkeypatch, LONG_TEXT)
    _patch_pages(monkeypatch, {"https://news.example.com": OG_HTML})
    feed, article = await _article(db_session)

    async with db_session() as s:
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.image_url == "https://img.example.com/og.jpg"
        detail = await s.scalar(
            select(ActivityEvent.detail).where(ActivityEvent.action == "fulltext_fetch")
        )
        assert detail is not None and '"image_recovered": true' in detail


async def test_fulltext_keeps_feed_image(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """An image already provided by the feed is never overwritten by og:image."""
    _patch_extract(monkeypatch, LONG_TEXT)
    _patch_pages(monkeypatch, {"https://news.example.com": OG_HTML})
    feed, article = await _article(db_session)

    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        a.image_url = "https://img.example.com/from-feed.jpg"
        await s.commit()
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.image_url == "https://img.example.com/from-feed.jpg"


# --- page publication-date recovery (mail feeds) ---

DATED_HTML = (
    '<html><head><meta property="article:published_time" content="2026-08-20T10:30:00Z">'
    "</head><body>article</body></html>"
)


async def test_fulltext_recovers_page_date_for_mail_feed(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mail articles start with the email Date: header (delivery time); the
    linked page's real publication date replaces it during full-text fetch."""
    _patch_extract(monkeypatch, LONG_TEXT)
    _patch_pages(monkeypatch, {"https://news.example.com": DATED_HTML})
    feed, article = await _article(db_session, kind="mail", sender_email="news@example.com")
    email_date = datetime.now(UTC) - timedelta(hours=2)

    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        a.published_at = email_date
        await s.commit()
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.published_at is not None
        assert a.published_at.date().isoformat() == "2026-08-20"
        detail = await s.scalar(
            select(ActivityEvent.detail).where(ActivityEvent.action == "fulltext_fetch")
        )
        assert detail is not None and '"date_recovered": true' in detail


async def test_fulltext_keeps_feedparser_date_for_rss(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RSS entries keep the feedparser date — page metadata never overrides it."""
    _patch_extract(monkeypatch, LONG_TEXT)
    _patch_pages(monkeypatch, {"https://news.example.com": DATED_HTML})
    feed, article = await _article(db_session)  # kind="rss"
    feed_date = datetime.now(UTC) - timedelta(hours=2)

    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        a.published_at = feed_date
        await s.commit()
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.published_at == feed_date
        detail = await s.scalar(
            select(ActivityEvent.detail).where(ActivityEvent.action == "fulltext_fetch")
        )
        assert detail is not None and '"date_recovered": false' in detail


def test_explicit_page_date_parsing() -> None:
    """Strict extractor: explicit metadata only — og/article meta + JSON-LD."""
    og = (
        '<html><head><meta property="article:published_time" '
        'content="2026-08-20T10:30:00Z"></head></html>'
    )
    assert fulltext._explicit_page_date(og) == datetime(2026, 8, 20, 10, 30, tzinfo=UTC)
    # attribute order reversed (content before property)
    rev = '<html><head><meta content="2026-08-21" name="date"></head></html>'
    assert fulltext._explicit_page_date(rev) == datetime(2026, 8, 21, tzinfo=UTC)
    # JSON-LD (and @graph nesting)
    ld = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@graph":[{"@type":"NewsArticle",'
        '"datePublished":"2026-08-22T08:00:00+02:00"}]}'
        "</script></head></html>"
    )
    assert fulltext._explicit_page_date(ld) == datetime(2026, 8, 22, 6, 0, tzinfo=UTC)


def test_explicit_page_date_rejects_guesses() -> None:
    """In doubt → no date: URL slugs, copyright years, bare text dates and
    bogus/future values are all ignored (the email date stays)."""
    assert fulltext._explicit_page_date("<html><body>nothing</body></html>") is None
    # trafilatura would guess 2026-01-01 from this copyright line — we must not
    assert (
        fulltext._explicit_page_date("<html><body><footer>© 2026 ACME</footer></body></html>")
        is None
    )
    # URL-looking date in an href is not metadata either
    assert (
        fulltext._explicit_page_date(
            '<html><body><a href="https://x.example/2023/11/03/post">old</a></body></html>'
        )
        is None
    )
    # garbage + future dates are rejected
    garbage = (
        '<html><head><meta property="article:published_time" '
        'content="not-a-date"></head></html>'
    )
    assert fulltext._explicit_page_date(garbage) is None
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    assert (
        fulltext._explicit_page_date(
            f'<html><head><meta property="article:published_time" content="{future}"></head></html>'
        )
        is None
    )


async def test_fulltext_keeps_email_date_without_explicit_metadata(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page has no explicit date metadata → the email Date: header stays."""
    _patch_extract(monkeypatch, LONG_TEXT)
    _patch_pages(monkeypatch, {"https://news.example.com": "<html><body>plain</body></html>"})
    feed, article = await _article(db_session, kind="mail", sender_email="news@example.com")
    email_date = datetime.now(UTC) - timedelta(hours=2)

    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        a.published_at = email_date
        await s.commit()
        await _run_fulltext(s, feed, article)
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.published_at == email_date
        detail = await s.scalar(
            select(ActivityEvent.detail).where(ActivityEvent.action == "fulltext_fetch")
        )
        assert detail is not None and '"date_recovered": false' in detail


async def test_reprocess_article_endpoint(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /stories/articles/{id}/reprocess re-runs the fulltext chain on demand."""
    from httpx import AsyncClient
    from tests.conftest import setup_admin

    assert isinstance(client, AsyncClient)
    _patch_extract(monkeypatch, LONG_TEXT)
    _patch_pages(monkeypatch, {"https://news.example.com": "<html>full now</html>"})
    _feed, article = await _article(db_session)
    article.full_text = "rss excerpt"
    article.content_status = "partial"
    article.content_warning = "blocked by robots.txt or site unreachable"

    # vec tables are migration-only; swap the store for a no-op stub
    from app.api import stories as stories_api

    class _NoopStore:
        async def delete_article(self, article_id: int) -> None: ...

    monkeypatch.setattr(stories_api, "get_vector_store", lambda session: _NoopStore())

    await setup_admin(client)
    r = await client.post(f"/api/stories/articles/{article.id}/reprocess")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content_status"] == "full"
    assert body["chars"] == len(LONG_TEXT)
    assert body["requeued"] is True

    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        assert a.full_text == LONG_TEXT
        assert a.processing_state == "fulltext"  # back through the LLM pipeline


def test_strip_archive_chrome_keeps_only_snapshot() -> None:
    html = (
        '<html><body><div id="HEADER">archive toolbar</div><div id="DIVSHARE">share</div>'
        '<div id="CONTENT"><article><p>Real story</p></article></div></body></html>'
    )
    out = fulltext._strip_archive_chrome(html)
    assert "Real story" in out
    assert "toolbar" not in out and "share" not in out


def test_strip_archive_chrome_without_content_div_drops_known_chrome() -> None:
    html = '<html><body><div id="HEADER">toolbar</div><p>Body</p></body></html>'
    out = fulltext._strip_archive_chrome(html)
    assert "Body" in out and "toolbar" not in out


async def test_archive_requests_use_browser_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    class FakeClient:
        def __init__(self, *a: object, **k: object) -> None: ...
        async def __aenter__(self) -> "FakeClient":
            return self
        async def __aexit__(self, *a: object) -> None: ...
        async def get(self, url: str, headers: dict[str, str], cookies: object = None):  # type: ignore[no-untyped-def]
            seen[url] = headers["User-Agent"]
            return httpx.Response(200, text="<html></html>")

    monkeypatch.setattr(fulltext.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(fulltext, "_robots_allowed_sync", lambda url: True)
    archive_url = "https://archive.is/newest/https%3A%2F%2Fx.example%2Fa"
    await fulltext._fetch_page(archive_url)
    await fulltext._fetch_page("https://news.example.com/a")
    assert seen[archive_url] == fulltext.ARCHIVE_USER_AGENT
    assert seen["https://news.example.com/a"] == fulltext.USER_AGENT
