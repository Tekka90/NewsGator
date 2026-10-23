"""Video articles (VIDEO_ARTICLES_PLAN): embedded/page videos through fulltext, ingest and API."""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.models import ActivityEvent, Article, Feed, Story, UserFeed
from app.services import fulltext, ingest, video_detect, youtube

PAGES = Path(__file__).parent / "fixtures" / "video_pages"
VID = "dQw4w9WgXcQ"
LONG_BLURB = "Context paragraph. " * 40


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    fulltext._archive_failures.clear()
    fulltext._domain_last_fetch.clear()
    fulltext._robots_cache.clear()


def _patch_page(monkeypatch: pytest.MonkeyPatch, html: str, extracted: str | None) -> None:
    async def fake_fetch(url: str, cookies: dict[str, str] | None = None) -> str | None:
        return html

    monkeypatch.setattr(fulltext, "_fetch_page", fake_fetch)
    monkeypatch.setattr(fulltext, "_extract_text", lambda h: extracted)


def _result(outcome: youtube.Outcome, text: str = "", **info: Any) -> youtube.VideoResult:
    pinfo = youtube.PlayerInfo(video_id=VID, title="Real title", **info)
    track = youtube.CaptionTrack("en", "u") if outcome is youtube.Outcome.TRANSCRIPT else None
    return youtube.VideoResult(outcome, pinfo, text=text, track=track, reason=outcome.value)


async def _news_article(db_session, url: str = "https://f24.my/CEi8", **kw: Any) -> int:
    async with db_session() as s:
        feed = Feed(url="https://news.example.com/rss", title="News")
        s.add(feed)
        await s.flush()
        art = Article(
            feed_id=feed.id,
            guid=url,
            url=url,
            title="Aid trucks",
            raw_content="<p>RSS excerpt of the report.</p>",
            processing_state="fetched",
            **kw,
        )
        s.add(art)
        await s.commit()
        return art.id


async def _run(
    db_session, monkeypatch, article_id: int, result: youtube.VideoResult | Exception | None
) -> tuple[Article, list[dict]]:
    calls: list[dict[str, Any]] = []

    async def fake_fetch_video(video_id: str, **kw: Any) -> youtube.VideoResult:
        calls.append({"id": video_id, **kw})
        if isinstance(result, Exception):
            raise result
        assert result is not None
        return result

    monkeypatch.setattr(youtube, "fetch_video", fake_fetch_video)
    async with db_session() as s:
        article = await s.get(Article, article_id)
        feed = await s.get(Feed, article.feed_id)
        await fulltext.fetch_full_text(s, article, feed)
    async with db_session() as s:
        article = await s.get(Article, article_id)
        events = [
            json.loads(e.detail)
            for e in (
                await s.scalars(
                    select(ActivityEvent).where(ActivityEvent.action == "fulltext_fetch")
                )
            ).all()
        ]
    article.__dict__["_calls"] = calls
    return article, events


F24 = (PAGES / "france24_cei8.html").read_text(encoding="utf-8")


async def test_france24_page_is_flagged_and_transcribed(db_session, monkeypatch) -> None:
    _patch_page(monkeypatch, F24, "Short page text.")
    aid = await _news_article(db_session)
    article, events = await _run(
        db_session,
        monkeypatch,
        aid,
        _result(youtube.Outcome.TRANSCRIPT, "Spoken words.", length_seconds=52),
    )
    assert article.media_kind == "video" and article.video_provider == "youtube"
    assert article.video_id == VID
    assert article.video_url == f"https://www.youtube.com/watch?v={VID}"
    assert article.video_embed_url == f"https://www.youtube-nocookie.com/embed/{VID}"
    assert article.video_duration == 52
    assert "Short page text." in article.full_text and "Spoken words." in article.full_text
    assert article.full_text.index("Short page text.") < article.full_text.index("Spoken words.")
    assert article.title == "Aid trucks"  # embedded video never overrides the page title
    assert article.content_status == "full" and article.processing_state == "fulltext"
    assert events[-1]["path"] == "video_embedded_transcript"


async def test_embedded_short_video_is_not_skipped(db_session, monkeypatch) -> None:
    _patch_page(monkeypatch, F24, None)
    aid = await _news_article(db_session)
    short = _result(youtube.Outcome.TRANSCRIPT, "Words.", length_seconds=20)
    article, events = await _run(db_session, monkeypatch, aid, short)
    assert article.content_status != "skipped" and article.processing_state == "fulltext"
    assert article.__dict__["_calls"][0]["allow_short"] is True
    assert events[-1]["path"] == "video_embedded_transcript"


async def test_embedded_no_captions_uses_description(db_session, monkeypatch) -> None:
    html = (PAGES / "youtube_nocookie_iframe.html").read_text(encoding="utf-8")
    _patch_page(monkeypatch, html, None)
    aid = await _news_article(db_session)
    article, events = await _run(
        db_session, monkeypatch, aid, _result(youtube.Outcome.NO_CAPTIONS, "Video description")
    )
    assert "Video description" in article.full_text
    assert article.content_status == "partial" and article.content_warning
    assert article.media_kind == "video"
    assert events[-1]["path"] == "video_embedded_description"


async def test_embedded_unplayable_keeps_flag_and_page_text(db_session, monkeypatch) -> None:
    _patch_page(monkeypatch, F24, "Page blurb text.")
    aid = await _news_article(db_session)
    article, events = await _run(
        db_session, monkeypatch, aid, _result(youtube.Outcome.UNPLAYABLE)
    )
    assert article.media_kind == "video" and article.video_id == VID
    assert article.content_status == "partial"
    assert "Page blurb text." in article.full_text
    assert events[-1]["path"] == "video_embedded_unavailable"


@pytest.mark.parametrize("outcome", [youtube.Outcome.LIVE, youtube.Outcome.UPCOMING])
async def test_embedded_live_is_retried_later(db_session, monkeypatch, outcome) -> None:
    _patch_page(monkeypatch, F24, None)
    aid = await _news_article(db_session)
    article, _ = await _run(db_session, monkeypatch, aid, _result(outcome))
    assert article.processing_state == "fetched"


async def test_embedded_transient_error_stays_fetched(db_session, monkeypatch) -> None:
    _patch_page(monkeypatch, F24, None)
    aid = await _news_article(db_session)
    exc = youtube.YouTubeError(youtube.ErrorCode.BLOCKED, "captcha")
    article, events = await _run(db_session, monkeypatch, aid, exc)
    assert article.processing_state == "fetched"
    assert events[-1]["error_code"] == "blocked"


async def test_non_youtube_page_video_is_flagged_without_transcript(
    db_session, monkeypatch
) -> None:
    html = (PAGES / "vimeo_iframe.html").read_text(encoding="utf-8")
    _patch_page(monkeypatch, html, "Tiny body.")
    aid = await _news_article(db_session, "https://news.example.com/vimeo")
    article, events = await _run(db_session, monkeypatch, aid, None)
    assert article.media_kind == "video" and article.video_provider == "embed"
    assert article.video_id is None
    assert article.video_embed_url.startswith("https://player.vimeo.com/video/")
    assert article.content_status == "partial" and article.content_warning
    assert article.processing_state == "fulltext"
    assert events[-1]["path"] == "video_embedded_description"


async def test_native_video_file_is_flagged(db_session, monkeypatch) -> None:
    html = (PAGES / "native_video_mp4.html").read_text(encoding="utf-8")
    _patch_page(monkeypatch, html, "x " * 40)
    aid = await _news_article(db_session, "https://example.com/a/b")
    article, _ = await _run(db_session, monkeypatch, aid, None)
    assert article.media_kind == "video" and article.video_provider == "file"
    assert article.video_url == "https://example.com/media/launch.mp4?token=a&b=2"


async def test_text_article_with_small_embed_is_not_flagged(db_session, monkeypatch) -> None:
    html = (PAGES / "text_article_small_embed.html").read_text(encoding="utf-8")
    _patch_page(monkeypatch, html, "Long article body. " * 150)
    aid = await _news_article(db_session, "https://news.example.com/text")
    article, _ = await _run(db_session, monkeypatch, aid, None)
    assert article.media_kind is None and article.video_provider is None
    assert article.content_status == "full"


async def test_plain_page_is_not_flagged(db_session, monkeypatch) -> None:
    html = (PAGES / "no_video.html").read_text(encoding="utf-8")
    _patch_page(monkeypatch, html, "Plain body. " * 60)
    aid = await _news_article(db_session, "https://news.example.com/plain")
    article, _ = await _run(db_session, monkeypatch, aid, None)
    assert article.media_kind is None


async def test_detection_failure_never_breaks_fulltext(db_session, monkeypatch) -> None:
    _patch_page(monkeypatch, F24, "Plain body. " * 60)

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(video_detect, "find_video", boom)
    aid = await _news_article(db_session)
    article, _ = await _run(db_session, monkeypatch, aid, None)
    assert article.media_kind is None and article.content_status == "full"


# --- ingest: native YouTube items persist the flag -----------------------------------------


RSS_YT = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Clip</title><link>https://www.youtube.com/watch?v={VID}</link><guid>y1</guid></item>
<item><title>Text</title><link>https://news.example.com/t</link><guid>t1</guid></item>
</channel></rss>""".encode()


async def test_poll_persists_flag_for_youtube_links_only(db_session, monkeypatch) -> None:
    async def fake_http(feed: Feed) -> tuple[int, bytes, dict[str, str]]:
        return 200, RSS_YT, {}

    monkeypatch.setattr(ingest, "_http_get", fake_http)
    async with db_session() as s:
        feed = Feed(url="https://news.example.com/rss", fetch_fulltext=False)
        s.add(feed)
        await s.commit()
        assert await ingest.poll_feed(s, await s.get(Feed, feed.id)) == 2
        by_guid = {a.guid: a for a in (await s.scalars(select(Article))).all()}
    yt = by_guid["y1"]
    assert (yt.media_kind, yt.video_provider, yt.video_id) == ("video", "youtube", VID)
    assert yt.video_embed_url == f"https://www.youtube-nocookie.com/embed/{VID}"
    assert by_guid["t1"].media_kind is None and by_guid["t1"].video_url is None


# --- API ---------------------------------------------------------------------------------


async def _seed_story(db_session, user_id: int, **video: Any) -> tuple[int, int]:
    async with db_session() as s:
        feed = Feed(url=f"https://x.example/{len(video)}.xml", title="F")
        s.add(feed)
        await s.flush()
        s.add(UserFeed(user_id=user_id, feed_id=feed.id))
        art = Article(
            feed_id=feed.id,
            guid="g",
            url="https://f24.my/CEi8",
            title="Aid",
            processing_state="clustered",
            **video,
        )
        s.add(art)
        await s.flush()
        story = Story(title="Aid", summary="S", category="World")
        s.add(story)
        await s.flush()
        art.story_id = story.id
        await s.commit()
        return story.id, art.id


VIDEO_KEYS = ("media_kind", "video_id", "video_url", "video_embed_url", "video_provider")


async def test_api_exposes_video_fields_and_video_preset(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    sid, _ = await _seed_story(
        db_session,
        1,
        **video_detect.video_fields(video_detect.detect_video(F24, "https://f24.my/CEi8", 0)),
    )
    listing = (await client.get("/api/stories")).json()
    item = next(i for i in _items(listing) if i["id"] == sid)
    assert item["media_kind"] == "video" and item["video_id"] == VID
    assert item["video_provider"] == "youtube" and item["video_duration"] == 52
    assert item["video_embed_url"].endswith(VID)
    detail = (await client.get(f"/api/stories/{sid}")).json()
    assert detail["video_url"] == f"https://www.youtube.com/watch?v={VID}"
    assert detail["articles"][0]["video_provider"] == "youtube"
    assert detail["articles"][0]["media_kind"] == "video"


async def test_api_non_video_story_has_null_video_fields(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    sid, _ = await _seed_story(db_session, 1)
    item = next(i for i in _items((await client.get("/api/stories")).json()) if i["id"] == sid)
    for key in (*VIDEO_KEYS, "video_duration"):
        assert key in item and item[key] is None


def _items(payload: Any) -> list[dict[str, Any]]:
    return payload["items"] if isinstance(payload, dict) else payload
