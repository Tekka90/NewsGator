# ruff: noqa: E501
"""fulltext helpers: date metadata trust rules, archive chrome stripping, cookies, batch isolation."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.models import ActivityEvent, Article, Feed
from app.services import fulltext


def test_parse_cookies_ignores_malformed_pairs() -> None:
    assert fulltext._parse_cookies(None) is None
    assert fulltext._parse_cookies("") is None
    assert fulltext._parse_cookies("novalue; ;") is None
    assert fulltext._parse_cookies(" a=1; b = two ; broken; c=x=y") == {
        "a": "1",
        "b": "two",
        "c": "x=y",
    }


def test_parse_page_date_rules() -> None:
    assert fulltext._parse_page_date(None) is None
    assert fulltext._parse_page_date("not a date") is None
    naive = fulltext._parse_page_date("2026-03-04T10:00:00")
    assert naive == datetime(2026, 3, 4, 10, tzinfo=UTC)
    assert fulltext._parse_page_date(" 2026-03-04T10:00:00Z ") == naive
    future = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    assert fulltext._parse_page_date(future) is None  # clock-skewed CMS
    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    assert fulltext._parse_page_date(soon) is not None  # within a day tolerated


def test_explicit_page_date_picks_earliest_trusted_candidate() -> None:
    html = """
    <meta property="article:published_time" content="2026-05-02T08:00:00Z">
    <meta content="2026-05-01T08:00:00Z" name="DC.Date">
    <meta name="keywords" content="2020-01-01">
    <meta property="og:title" content="2019-01-01">
    <script type="application/ld+json">{"@graph": [{"datePublished": "2026-06-01T00:00:00Z"}]}</script>
    <script type="application/ld+json">{broken json</script>
    """
    assert fulltext._explicit_page_date(html) == datetime(2026, 5, 1, 8, tzinfo=UTC)
    assert fulltext._explicit_page_date("<p>© 2024, /2023/11/03/slug</p>") is None
    only_ld = '<script type="application/ld+json">[{"dateCreated": "2026-01-01"}, 5]</script>'
    assert fulltext._explicit_page_date(only_ld) == datetime(2026, 1, 1, tzinfo=UTC)


def test_jsonld_dates_recurses_and_ignores_non_strings() -> None:
    node = {"datePublished": "a", "x": [{"dateCreated": "b"}, {"datePublished": 5}], "y": "z"}
    assert sorted(fulltext._jsonld_dates(node)) == ["a", "b"]
    assert fulltext._jsonld_dates("scalar") == []


def test_strip_archive_chrome_keeps_snapshot_or_drops_toolbars() -> None:
    wrapped = (
        '<html><body><div id="HEADER">h</div><div id="CONTENT"><p>Story</p></div></body></html>'
    )
    out = fulltext._strip_archive_chrome(wrapped)
    assert "Story" in out and "HEADER" not in out
    no_content = (
        '<html><body><div id="HEADER">bar</div><div id="DIVSHARE">share</div>'
        '<div id="SOLID">x</div><p>Body</p></body></html>'
    )
    out = fulltext._strip_archive_chrome(no_content)
    assert "Body" in out and "bar" not in out and "share" not in out
    assert fulltext._strip_archive_chrome("") == ""  # unparsable → untouched


def test_extract_page_meta_handles_missing_and_failing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trafilatura

    html = (
        '<html><head><meta property="og:image" content="https://i.example/a.jpg">'
        '<meta property="article:published_time" content="2026-01-02"></head>'
        "<body><p>x</p></body></html>"
    )
    image, date = fulltext._extract_page_meta(html)
    assert image == "https://i.example/a.jpg" and date is not None and "2026-01-02" in date

    monkeypatch.setattr(trafilatura, "extract_metadata", lambda _h: None)
    assert fulltext._extract_page_meta(html) == (None, None)

    def boom(_h: str) -> None:
        raise ValueError("parse")

    monkeypatch.setattr(trafilatura, "extract_metadata", boom)
    assert fulltext._extract_page_meta(html) == (None, None)


def test_extract_text_falls_back_to_readability(monkeypatch: pytest.MonkeyPatch) -> None:
    import trafilatura

    calls: list[str] = []

    def fake_extract(html: str, *a: Any, **k: Any) -> str | None:
        calls.append(html)
        return None if len(calls) == 1 else "from readability"

    monkeypatch.setattr(trafilatura, "extract", fake_extract)
    html = "<html><body><article><p>" + "Hello world. " * 40 + "</p></article></body></html>"
    assert fulltext._extract_text(html) == "from readability"
    assert len(calls) == 2

    monkeypatch.setattr(trafilatura, "extract", lambda *_a, **_k: None)
    assert fulltext._extract_text(html) is None

    import readability

    monkeypatch.setattr(readability, "Document", lambda _h: (_ for _ in ()).throw(ValueError()))
    assert fulltext._extract_text(html) is None  # readability crash is swallowed


def test_looks_paywalled_and_plain_text() -> None:
    assert fulltext._looks_paywalled("Please SUBSCRIBE TO CONTINUE reading")
    assert not fulltext._looks_paywalled("free article")
    assert fulltext._plain_text("<p>A&amp;B</p>\n<b>c</b>") == "A&B c"
    assert fulltext._plain_text(None) == ""
    assert fulltext._archive_url("https://x.example/a?b=c d") == (
        "https://archive.is/newest/https%3A%2F%2Fx.example%2Fa%3Fb%3Dc%20d"
    )


async def test_fetch_batch_isolates_failures_and_skips_missing(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session() as s:
        feed = Feed(url="https://f.example/rss", title="F")
        s.add(feed)
        await s.flush()
        arts = [
            Article(feed_id=feed.id, guid=f"g{i}", url=f"https://x/{i}", title=f"t{i}")
            for i in range(3)
        ]
        s.add_all(arts)
        await s.commit()
        ids, fid = [a.id for a in arts], feed.id

    async def fake_fetch(_s: Any, article: Article, _feed: Feed) -> None:
        if article.title == "t1":
            raise RuntimeError("fetch exploded")

    monkeypatch.setattr(fulltext, "fetch_full_text", fake_fetch)
    assert await fulltext.fetch_full_text_batch(fid, []) == []
    ready = await fulltext.fetch_full_text_batch(fid, [*ids, 99999])
    assert sorted(ready) == sorted([ids[0], ids[2]])  # failed + missing are not handed off
    assert await fulltext.fetch_full_text_batch(424242, [ids[0]]) == []  # unknown feed

    async with db_session() as s:
        events = (await s.scalars(select(ActivityEvent))).all()
    errors = [e for e in events if e.level == "error"]
    assert len(errors) == 1 and "fetch exploded" in errors[0].detail
    assert f'"article_id": {ids[1]}' in errors[0].detail
