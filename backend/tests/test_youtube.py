"""YouTube: URL rules, player/caption parsing, filtering, fulltext branch, feeds, Takeout import,
oEmbed at import, plus the Phase 1A context budgeting / map-reduce condensation."""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.core.config import settings
from app.models import ActivityEvent, Article, Feed, LLMUsage, Story, UserFeed
from app.services import (
    condense,
    fulltext,
    llm_budget,
    llm_client,
    process,
    video_detect,
    youtube,
)
from app.services.ingest import canonicalize_url

FIXTURES = Path(__file__).parent / "fixtures"
VECTORS = json.loads((FIXTURES / "youtube_vectors.json").read_text(encoding="utf-8"))
VID = "dQw4w9WgXcQ"
CHANNEL_ID = "UC" + "a" * 22


# --- shared vectors (also copied by the Swift side) ---------------------------------------


def test_vectors_file_is_well_formed() -> None:
    assert set(VECTORS) >= {"version", "urls", "caption_xml", "player", "takeout", "constants"}
    for key in ("urls", "caption_xml", "player", "takeout"):
        assert VECTORS[key], key


@pytest.mark.parametrize("case", VECTORS["urls"], ids=lambda c: c["name"])
def test_url_vectors(case: dict[str, Any]) -> None:
    assert youtube.canonical_url(case["input"]) == case["canonical"]
    assert youtube.parse_video_id(case["input"]) == case["video_id"]
    if case["video_id"]:
        assert youtube.start_offset(case["input"]) == case["start_seconds"]
        assert canonicalize_url(case["input"]) == case["canonical"]


@pytest.mark.parametrize("case", VECTORS["caption_xml"], ids=lambda c: c["name"])
def test_caption_vectors(case: dict[str, Any]) -> None:
    text = youtube.build_paragraphs(youtube.parse_caption_xml(case["xml"]))
    assert text == case["paragraphs"]
    for paragraph in text.split("\n\n")[:-1]:
        assert len(paragraph) <= youtube.PARAGRAPH_MAX_CHARS + 120


@pytest.mark.parametrize("case", VECTORS["player"], ids=lambda c: c["name"])
def test_player_vectors(case: dict[str, Any]) -> None:
    info = youtube.parse_player_response(VID, case["response"])
    assert youtube.decide(info).value == case["verdict"]
    track = youtube.choose_track(info.tracks, info.original_language)
    expected = case["chosen_track"]
    if expected is None:
        assert track is None
    else:
        assert track is not None
        assert (track.language_code, track.kind) == (expected["language_code"], expected["kind"])


@pytest.mark.parametrize("case", VECTORS["takeout"], ids=lambda c: c["name"])
def test_takeout_vectors(case: dict[str, Any]) -> None:
    rows = youtube.parse_takeout_csv(case["csv"].encode("utf-8"))
    assert [{"title": t, "url": u} for t, u in rows.feeds] == case["feeds"]
    assert rows.invalid == case["invalid"]


def test_takeout_without_channel_id_column_is_rejected() -> None:
    with pytest.raises(ValueError):
        youtube.parse_takeout_csv(b"a,b\n1,2\n")


# --- real captured fixtures -------------------------------------------------------------


def test_real_player_response_picks_original_language() -> None:
    data = json.loads((FIXTURES / "youtube_player_real.json").read_text())
    info = youtube.parse_player_response("xGZdqOOyxJM", data)
    track = youtube.choose_track(info.tracks, info.original_language)
    assert track is not None and track.language_code == "fr"
    assert youtube.decide(info) is youtube.Verdict.OK


def test_real_caption_xml_parses_to_clean_paragraphs() -> None:
    xml = (FIXTURES / "youtube_caption_real_srv3.xml").read_text(encoding="utf-8")
    segments = youtube.parse_caption_xml(xml)
    assert segments
    text = youtube.build_paragraphs(segments)
    assert "[" not in text and "&amp;" not in text and "&#39;" not in text


# --- channel resolution ----------------------------------------------------------------


def test_channel_page_parsing_prefers_rss_link() -> None:
    page = (
        '<meta property="og:title" content="My &amp; Channel">'
        f'<link rel="alternate" type="application/rss+xml" title="RSS" '
        f'href="https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}">'
    )
    channel = youtube.parse_channel_page(page)
    assert channel is not None
    assert (channel.channel_id, channel.title) == (CHANNEL_ID, "My & Channel")
    assert youtube.parse_channel_page("<html>nothing</html>") is None


def test_channel_input_detection() -> None:
    for yes in (CHANNEL_ID, "@handle", "https://www.youtube.com/@x", "youtube.com/c/x", VID):
        assert youtube.is_channel_input(yes) == (yes != VID)
    assert not youtube.is_channel_input("https://example.com/feed.xml")
    assert youtube.is_channel_feed_url(youtube.feed_url_for_channel(CHANNEL_ID))
    assert not youtube.is_channel_feed_url("https://www.youtube.com/watch?v=" + VID)


# --- fulltext branch ---------------------------------------------------------------------


async def _video_article(db_session, url: str = f"https://www.youtube.com/watch?v={VID}") -> int:
    async with db_session() as s:
        feed = Feed(url=youtube.feed_url_for_channel(CHANNEL_ID), title="Chan")
        s.add(feed)
        await s.flush()
        art = Article(
            feed_id=feed.id,
            guid=url,
            url=url,
            title="www.youtube.com",
            raw_content="RSS description",
            processing_state="fetched",
            **video_detect.youtube_video_fields(url),
        )
        s.add(art)
        await s.commit()
        return art.id


def _result(outcome: youtube.Outcome, text: str = "", **info_kwargs: Any) -> youtube.VideoResult:
    info = youtube.PlayerInfo(video_id=VID, title="Real title", **info_kwargs)
    track = youtube.CaptionTrack("en", "u") if outcome is youtube.Outcome.TRANSCRIPT else None
    return youtube.VideoResult(outcome, info, text=text, track=track, reason=outcome.value)


async def _run_fulltext(db_session, monkeypatch, result_or_exc) -> tuple[Article, list[dict]]:
    article_id = await _video_article(db_session)

    async def fake(video_id: str, **_: Any) -> youtube.VideoResult:
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(youtube, "fetch_video", fake)
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
    return article, events


async def test_fulltext_transcript(db_session, monkeypatch) -> None:
    article, events = await _run_fulltext(
        db_session, monkeypatch, _result(youtube.Outcome.TRANSCRIPT, "Para one.\n\nPara two.")
    )
    assert article.full_text == "Para one.\n\nPara two."
    assert article.content_status == "full" and article.processing_state == "fulltext"
    assert article.title == "Real title"
    assert article.image_url == youtube.thumbnail_url(VID)
    assert events[-1]["path"] == "youtube_transcript"


async def test_fulltext_no_captions_uses_description(db_session, monkeypatch) -> None:
    article, events = await _run_fulltext(
        db_session, monkeypatch, _result(youtube.Outcome.NO_CAPTIONS, "Video description")
    )
    assert article.full_text == "Video description"
    assert article.content_status == "partial" and article.content_warning
    assert events[-1]["path"] == "youtube_description"


async def test_fulltext_unplayable_falls_back_to_rss_content(db_session, monkeypatch) -> None:
    article, events = await _run_fulltext(
        db_session, monkeypatch, _result(youtube.Outcome.UNPLAYABLE)
    )
    assert article.full_text == "RSS description"
    assert article.content_status == "partial"
    assert "unavailable" in (article.content_warning or "")
    assert events[-1]["path"] == "youtube_unavailable"


async def test_fulltext_short_is_dropped(db_session, monkeypatch) -> None:
    article, events = await _run_fulltext(
        db_session, monkeypatch, _result(youtube.Outcome.SHORT, length_seconds=30)
    )
    assert article.processing_state == "clustered" and article.story_id is None
    assert article.content_status == "skipped"
    assert events[-1]["path"] == "youtube_skipped"


@pytest.mark.parametrize("outcome", [youtube.Outcome.LIVE, youtube.Outcome.UPCOMING])
async def test_fulltext_live_and_upcoming_are_retried_later(
    db_session, monkeypatch, outcome: youtube.Outcome
) -> None:
    article, events = await _run_fulltext(db_session, monkeypatch, _result(outcome))
    assert article.processing_state == "fetched"
    assert events[-1]["path"] == "youtube_pending"


async def test_fulltext_error_keeps_state_and_logs_code(db_session, monkeypatch) -> None:
    exc = youtube.YouTubeError(youtube.ErrorCode.BLOCKED, "captcha")
    article, events = await _run_fulltext(db_session, monkeypatch, exc)
    assert article.processing_state == "fetched"
    assert events[-1]["path"] == "youtube_error" and events[-1]["error_code"] == "blocked"


# --- fetch_video through the HTTP seams ----------------------------------------------------


async def test_fetch_video_end_to_end_with_fixtures(monkeypatch) -> None:
    data = json.loads((FIXTURES / "youtube_player_real.json").read_text())
    xml = (FIXTURES / "youtube_caption_real_srv3.xml").read_text(encoding="utf-8")

    async def fake_player(video_id: str) -> youtube.PlayerInfo:
        return youtube.parse_player_response(video_id, data)

    async def fake_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        return 200, xml

    async def fake_head(url: str) -> int:
        return 404

    monkeypatch.setattr(youtube, "fetch_player_info", fake_player)
    monkeypatch.setattr(youtube, "_get_text", fake_get)
    monkeypatch.setattr(youtube, "_head_status", fake_head)
    result = await youtube.fetch_video("xGZdqOOyxJM")
    assert result.outcome is youtube.Outcome.TRANSCRIPT
    assert result.track is not None and result.track.language_code == "fr"
    assert result.text


async def test_fetch_video_short_detected_by_shorts_url(monkeypatch) -> None:
    data = json.loads((FIXTURES / "youtube_player_real.json").read_text())

    async def fake_player(video_id: str) -> youtube.PlayerInfo:
        return youtube.parse_player_response(video_id, data)

    async def fake_head(url: str) -> int:
        return 200  # /shorts/<id> does not redirect → it is a Short

    monkeypatch.setattr(youtube, "fetch_player_info", fake_player)
    monkeypatch.setattr(youtube, "_head_status", fake_head)
    assert (await youtube.fetch_video("xGZdqOOyxJM")).outcome is youtube.Outcome.SHORT


# --- add feed / discovery ----------------------------------------------------------------


async def test_add_feed_with_youtube_channel_resolves_to_rss(
    client: AsyncClient, monkeypatch
) -> None:
    await setup_admin(client)

    async def fake_resolve(raw: str) -> youtube.Channel:
        assert raw == "https://www.youtube.com/@chan"
        return youtube.Channel(CHANNEL_ID, "Chan")

    monkeypatch.setattr(youtube, "resolve_channel", fake_resolve)
    r = await client.post("/api/feeds", json={"url": "https://www.youtube.com/@chan"})
    assert r.status_code == 201, r.text
    assert r.json()["url"] == youtube.feed_url_for_channel(CHANNEL_ID)


async def test_add_feed_with_unresolvable_youtube_input_is_422(
    client: AsyncClient, monkeypatch
) -> None:
    await setup_admin(client)

    async def fake_resolve(raw: str) -> youtube.Channel:
        raise youtube.YouTubeError(youtube.ErrorCode.UNAVAILABLE, "nope")

    monkeypatch.setattr(youtube, "resolve_channel", fake_resolve)
    r = await client.post("/api/feeds", json={"url": "@ghost"})
    assert r.status_code == 422


async def test_add_feed_still_validates_plain_urls(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post("/api/feeds", json={"url": "not a url"})
    assert r.status_code == 422


async def test_probe_resolves_youtube_channel(client: AsyncClient, monkeypatch) -> None:
    await setup_admin(client)

    async def fake_resolve(raw: str) -> youtube.Channel:
        return youtube.Channel(CHANNEL_ID, "Chan")

    monkeypatch.setattr(youtube, "resolve_channel", fake_resolve)
    r = await client.post("/api/feeds/probe", json={"url": "@chan"})
    assert r.status_code == 200, r.text
    assert r.json()["feed"]["url"] == youtube.feed_url_for_channel(CHANNEL_ID)


# --- Takeout import ----------------------------------------------------------------------

TAKEOUT = (
    "\ufeffChannel ID,Channel URL,Channel title\r\n"
    f'{CHANNEL_ID},http://www.youtube.com/channel/{CHANNEL_ID},"Smith, John & Co"\r\n'
    f"UC{'b' * 22},http://www.youtube.com/channel/UC{'b' * 22},"
    '"The ""Quoted"" Channel"\r\n'
    f"{CHANNEL_ID},http://www.youtube.com/channel/{CHANNEL_ID},Dup\r\n"
    "broken,http://x,Bad\r\n"
)


async def test_import_subscriptions_takeout_csv(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    files = {"file": ("subscriptions.csv", TAKEOUT.encode("utf-8"), "text/csv")}
    r = await client.post("/api/feeds/import-subscriptions", files=files)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["added"], body["skipped"], body["invalid"]) == (2, 0, 1)
    assert {f["title"] for f in body["feeds"]} == {"Smith, John & Co", 'The "Quoted" Channel'}
    assert all(f["url"].startswith(youtube.CHANNEL_FEED_BASE) for f in body["feeds"])

    again = await client.post("/api/feeds/import-subscriptions", files=files)
    assert (again.json()["added"], again.json()["skipped"]) == (0, 2)


async def test_import_subscriptions_accepts_opml_and_rejects_garbage(client: AsyncClient) -> None:
    await setup_admin(client)
    opml = (
        b'<?xml version="1.0"?><opml version="2.0"><body>'
        b'<outline text="A" xmlUrl="https://a.example.com/feed"/></body></opml>'
    )
    r = await client.post("/api/feeds/import-subscriptions", files={"file": ("s.opml", opml)})
    assert r.status_code == 201 and r.json()["added"] == 1
    bad = await client.post(
        "/api/feeds/import-subscriptions", files={"file": ("x.csv", b"foo,bar\n1,2\n")}
    )
    assert bad.status_code == 400


# --- shared links ------------------------------------------------------------------------


async def test_import_url_youtube_uses_oembed_and_canonical_url(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    await setup_admin(client)

    async def fake_oembed(url: str) -> youtube.OEmbed:
        assert url == f"https://www.youtube.com/watch?v={VID}"
        return youtube.OEmbed("A great talk", "The Channel", "https://i.ytimg.com/x.jpg")

    monkeypatch.setattr(youtube, "fetch_oembed", fake_oembed)
    r = await client.post(
        "/api/stories/import-url", json={"url": f"https://youtu.be/{VID}?si=track&t=30"}
    )
    assert r.status_code == 201, r.text
    async with db_session() as s:
        article = await s.get(Article, r.json()["article_id"])
        assert article is not None
        assert article.url == f"https://www.youtube.com/watch?v={VID}"
        assert article.title == "A great talk"
        assert article.origin_feed_title == "The Channel"
        assert article.image_url == "https://i.ytimg.com/x.jpg"


async def test_import_url_youtube_survives_oembed_failure(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    await setup_admin(client)

    async def fake_oembed(url: str) -> None:
        return None

    monkeypatch.setattr(youtube, "fetch_oembed", fake_oembed)
    r = await client.post("/api/stories/import-url", json={"url": f"https://youtu.be/{VID}"})
    assert r.status_code == 201
    async with db_session() as s:
        article = await s.get(Article, r.json()["article_id"])
        assert article is not None and article.image_url == youtube.thumbnail_url(VID)


async def test_video_dto_fields(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    article_id = await _video_article(db_session)
    async with db_session() as s:
        story = Story(title="Talk", summary="s", category="Tech")
        s.add(story)
        await s.flush()
        article = await s.get(Article, article_id)
        article.story_id = story.id
        article.processing_state = "clustered"
        s.add(UserFeed(user_id=1, feed_id=article.feed_id))
        await s.commit()
    detail = (await client.get("/api/stories/1")).json()
    assert detail["video_id"] == VID and detail["media_kind"] == "video"
    assert detail["articles"][0]["video_id"] == VID
    assert detail["articles"][0]["media_kind"] == "video"
    listed = (await client.get("/api/stories")).json()
    assert listed[0]["video_id"] == VID and listed[0]["media_kind"] == "video"


# --- Phase 1A: budgeting and condensation --------------------------------------------------


def test_budget_scales_with_context_tokens(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_context_tokens", 32768)
    small = llm_budget.input_budget_chars("sys", "fixed")
    monkeypatch.setattr(settings, "llm_context_tokens", 131072)
    assert llm_budget.input_budget_chars("sys", "fixed") > small * 3


def test_split_paragraphs_never_cuts_words() -> None:
    text = "\n\n".join(" ".join(f"w{i}x{j}" for j in range(80)) for i in range(20))
    chunks = llm_budget.split_paragraphs(text, 700)
    assert all(len(c) <= 700 for c in chunks)
    original = set(text.split())
    assert all(word in original for c in chunks for word in c.split())
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_sample_text_covers_the_whole_text() -> None:
    text = "\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(100))
    sampled = llm_budget.sample_text(text, 3000)
    assert len(sampled) <= 3000
    assert "paragraph 0 " in sampled and "paragraph 99 " in sampled


async def _article_for_condense(db_session) -> Article:
    article_id = await _video_article(db_session, "https://news.example.com/long")
    async with db_session() as s:
        article = await s.get(Article, article_id)
        assert article is not None
        return article


async def test_fit_text_returns_short_text_unchanged(db_session, monkeypatch) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("no LLM call expected")

    monkeypatch.setattr(llm_client, "chat_json", boom)
    article = await _article_for_condense(db_session)
    async with db_session() as s:
        out = await condense.fit_text(s, article, "short text " * 50, "sys", "user")
    assert out == "short text " * 50


async def test_fit_text_condenses_long_text_and_keeps_the_end(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_context_tokens", 4096)
    calls: list[str] = []

    async def fake_chat_json(system: str, user: str, **kw: Any) -> tuple[dict, int]:
        calls.append(user)
        return {"digest": "digest of " + user[-40:].strip()}, 5

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    text = "\n\n".join(f"Paragraph {i}. " + "lorem ipsum " * 60 for i in range(60))
    text += "\n\nFINAL-MARKER closing remarks."
    article = await _article_for_condense(db_session)
    async with db_session() as s:
        out = await condense.fit_text(s, article, text, "sys", "user")
        events = [e.action for e in (await s.scalars(select(ActivityEvent))).all()]
        kinds = {e.kind for e in (await s.scalars(select(LLMUsage))).all()}
    assert 2 <= len(calls) <= llm_budget.MAX_CALLS_PER_ARTICLE - 1
    assert len(out) <= llm_budget.input_budget_chars("sys", "user")
    assert "FINAL-MARKER" in "\n".join(calls)  # the end of the article is represented
    assert {"condense_start", "condense_done"} <= set(events)
    assert "condense" in kinds


async def test_condense_call_cap(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_context_tokens", 2048)
    count = 0

    async def fake_chat_json(system: str, user: str, **kw: Any) -> tuple[dict, int]:
        nonlocal count
        count += 1
        return {"digest": "d " * 400}, 1

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    text = "\n\n".join("sentence. " * 100 for _ in range(400))
    article = await _article_for_condense(db_session)
    async with db_session() as s:
        out = await condense.fit_text(s, article, text, "sys", "user")
    assert count <= llm_budget.MAX_CALLS_PER_ARTICLE - 1
    assert len(out) <= llm_budget.input_budget_chars("sys", "user")


async def test_condense_failure_leaves_article_retryable(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_context_tokens", 2048)

    async def failing(system: str, user: str, **kw: Any) -> tuple[dict, int]:
        raise llm_client.LLMError("down")

    monkeypatch.setattr(llm_client, "chat_json", failing)
    article_id = await _video_article(db_session, "https://news.example.com/long")
    async with db_session() as s:
        article = await s.get(Article, article_id)
        article.full_text = "word " * 20000
        article.processing_state = "fulltext"
        await s.commit()
        assert await process.summarize_article(s, article) is False
        assert article.processing_state == "fulltext"
        actions = [e.action for e in (await s.scalars(select(ActivityEvent))).all()]
    assert "condense_error" in actions and "summarize_error" in actions


async def test_video_summary_prompt_gets_media_guidance(db_session, monkeypatch) -> None:
    seen: list[str] = []

    async def fake_chat_json(system: str, user: str, **kw: Any) -> tuple[dict, int]:
        seen.append(user)
        return {"summary": "S", "category": "Tech"}, 1

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    article_id = await _video_article(db_session)
    async with db_session() as s:
        article = await s.get(Article, article_id)
        article.full_text = "Transcript of the talk. " * 30
        article.processing_state = "fulltext"
        await s.commit()
        assert await process.summarize_article(s, article) is True
    assert "transcript" in seen[0].lower() or "video" in seen[0].lower()


# --- llm_context_tokens setting ----------------------------------------------------------------


async def test_llm_context_tokens_setting_roundtrip_and_precedence(
    client: AsyncClient, monkeypatch
) -> None:
    await setup_admin(client)
    assert (await client.get("/api/settings")).json()["values"]["llm_context_tokens"] == 32768
    r = await client.patch("/api/settings", json={"values": {"llm_context_tokens": 65536}})
    assert r.status_code == 200, r.text
    assert r.json()["values"]["llm_context_tokens"] == 65536
    assert settings.llm_context_tokens == 65536
    bad = await client.patch("/api/settings", json={"values": {"llm_context_tokens": 100}})
    assert bad.status_code == 400
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "16384")
    got = (await client.get("/api/settings")).json()
    assert got["values"]["llm_context_tokens"] == 16384
    assert "llm_context_tokens" in got["env_locked"]
    await client.patch("/api/settings", json={"values": {"llm_context_tokens": None}})
