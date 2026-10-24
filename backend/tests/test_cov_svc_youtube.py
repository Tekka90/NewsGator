"""Coverage tests: YouTube service (HTTP seams, retry policy, player/captions, channels)."""

import json
import sys
import types
from typing import Any

import httpx
import pytest

from app.services import youtube
from app.services.youtube import ErrorCode, YouTubeError

_REAL_ASYNC = httpx.AsyncClient
UC = "UCabcdefghijklmnopqrstuv"


@pytest.fixture()
def http(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Route youtube's httpx client through a handler table. Each entry: callable(request)."""
    state: dict[str, Any] = {"handler": None, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["handler"](request)

    def make(**kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC(**kw)

    monkeypatch.setattr(youtube.httpx, "AsyncClient", make)
    return state


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(s: float) -> None:
        recorded.append(s)

    monkeypatch.setattr(youtube.asyncio, "sleep", fake_sleep)
    return recorded


# --- URL helpers ---


def test_video_url_helpers() -> None:
    assert youtube.is_video_url("https://youtu.be/abcdefghijk")
    assert not youtube.is_video_url("https://notyoutube.com/watch?v=abcdefghijk")
    assert youtube.canonical_url("https://www.youtube.com/shorts/abcdefghijk") == (
        "https://www.youtube.com/watch?v=abcdefghijk"
    )
    assert youtube.canonical_url("https://example.com/") is None
    assert youtube.parse_video_id("https://www.youtube.com/watch") is None
    assert youtube.parse_video_id("https://www.youtube.com/watch?v=short") is None
    assert youtube.parse_video_id("") is None
    assert (
        youtube.channel_id_from_feed_url(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={UC}"
        )
        == UC
    )
    assert (
        youtube.channel_id_from_feed_url("https://www.youtube.com/feeds/videos.xml?channel_id=bad")
        is None
    )


# --- HTTP seams ---


async def test_get_text_post_json_and_head(http) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "HEAD":
            return httpx.Response(302, headers={"location": "https://elsewhere"})
        if req.method == "POST":
            body = json.loads(req.content)
            return (
                httpx.Response(200, json={"echo": body["x"]})
                if body["x"] != "list"
                else httpx.Response(200, json=[1])
            )
        return httpx.Response(200, text="hello", headers={"x-ua": req.headers["user-agent"]})

    http["handler"] = handler
    assert await youtube._get_text("https://y.test/a", {"User-Agent": "UA-1"}) == (200, "hello")
    assert http["requests"][-1].headers["user-agent"] == "UA-1"
    await youtube._get_text("https://y.test/a")
    assert http["requests"][-1].headers["user-agent"] == youtube._WEB_HEADERS["User-Agent"]
    assert await youtube._post_json("https://y.test/p", {"x": 1}, {}) == (200, {"echo": 1})
    assert await youtube._post_json("https://y.test/p", {"x": "list"}, {}) == (
        200,
        {},
    )  # non-object
    assert await youtube._head_status("https://y.test/s") == 302  # redirects not followed

    http["handler"] = lambda req: httpx.Response(200, text="<html>")
    assert await youtube._post_json("https://y.test/p", {"x": 1}, {}) == (200, {})  # invalid JSON


# --- retry policy ---


async def test_retrying_returns_first_success_without_sleeping(sleeps) -> None:
    async def ok(*args: Any):
        return 404, "nope"

    assert await youtube._retrying(ok, "u") == (404, "nope")  # 4xx is not transient
    assert sleeps == []


async def test_retrying_backs_off_then_succeeds(sleeps) -> None:
    results = [httpx.ConnectError("x"), (503, ""), (200, "fine")]

    async def flaky(*args: Any):
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    assert await youtube._retrying(flaky) == (200, "fine")
    assert sleeps == list(youtube.RETRY_DELAYS_S)


@pytest.mark.parametrize(
    ("final", "code"),
    [
        ((429, ""), ErrorCode.BLOCKED),
        ((502, ""), ErrorCode.NETWORK),
        (httpx.ReadTimeout("t"), ErrorCode.NETWORK),
    ],
)
async def test_retrying_exhaustion_raises_typed_error(sleeps, final, code) -> None:
    calls = 0

    async def always(*args: Any):
        nonlocal calls
        calls += 1
        if isinstance(final, Exception):
            raise final
        return final

    with pytest.raises(YouTubeError) as ei:
        await youtube._retrying(always)
    assert ei.value.code is code
    assert calls == len(youtube.RETRY_DELAYS_S) + 1
    assert len(sleeps) == len(youtube.RETRY_DELAYS_S)  # no sleep after the last attempt


async def test_retrying_treats_non_tuple_results_as_ok(sleeps) -> None:
    async def no_tuple(*args: Any):
        return "plain"

    assert await youtube._retrying(no_tuple) == "plain"


# --- player parsing ---


def test_text_of_and_as_int() -> None:
    assert youtube._text_of("s") == "s"
    assert youtube._text_of({"simpleText": "Simple"}) == "Simple"
    assert youtube._text_of({"runs": [{"text": "a"}, {"text": "b"}, "junk"]}) == "ab"
    assert youtube._text_of({"runs": "no"}) == ""
    assert youtube._text_of(None) == ""
    assert youtube._as_int("12") == 12 and youtube._as_int(None) == 0 and youtube._as_int("x") == 0


def _player(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "videoDetails": {
            "title": "T",
            "author": "A",
            "channelId": UC,
            "shortDescription": "desc",
            "lengthSeconds": "300",
        },
        "playabilityStatus": {"status": "OK"},
        "captions": {
            "playerCaptionsTracklistRenderer": {
                "captionTracks": [
                    {
                        "baseUrl": "https://c/fr",
                        "languageCode": "fr",
                        "name": {"simpleText": "French"},
                    },
                    {"baseUrl": "https://c/en", "languageCode": "en", "kind": "asr"},
                    {"languageCode": "de"},  # no url -> dropped
                    "junk",
                ],
                "audioTracks": [{"captionTrackIndices": [0]}],
            }
        },
    }
    data.update(over)
    return data


def test_parse_player_response_tracks_and_original_language() -> None:
    info = youtube.parse_player_response("vid", _player())
    assert (info.title, info.channel, info.channel_id, info.length_seconds) == ("T", "A", UC, 300)
    assert [(t.language_code, t.is_auto, t.name) for t in info.tracks] == [
        ("fr", False, "French"),
        ("en", True, ""),
    ]
    assert info.original_language == "fr"
    tolerant = youtube.parse_player_response(
        "v", {"videoDetails": 3, "playabilityStatus": [], "captions": "x"}
    )
    assert (
        tolerant.tracks == []
        and tolerant.playability_status == "OK"
        and tolerant.length_seconds == 0
    )


def test_original_language_edge_cases() -> None:
    tracks = [youtube.CaptionTrack("fr", "u"), youtube.CaptionTrack("en", "u")]
    orl = youtube._original_language
    assert orl({}, tracks) is None
    assert orl({"audioTracks": []}, tracks) is None
    assert orl({"audioTracks": [{"captionTrackIndices": [1]}]}, tracks) == "en"
    # out-of-range default index falls back to the first audio track
    assert (
        orl({"audioTracks": [{"captionTrackIndices": [0]}], "defaultAudioTrackIndex": 9}, tracks)
        == "fr"
    )
    assert orl({"audioTracks": ["junk"], "defaultAudioTrackIndex": 0}, tracks) is None
    assert orl({"audioTracks": [{"captionTrackIndices": [7]}]}, tracks) is None
    assert orl({"audioTracks": [{"captionTrackIndices": []}]}, tracks) is None


def test_choose_track_priorities() -> None:
    manual_en = youtube.CaptionTrack("en", "m")
    auto_en = youtube.CaptionTrack("en", "a", kind="asr")
    manual_fr = youtube.CaptionTrack("fr-CA", "f")
    auto_de = youtube.CaptionTrack("de", "d", kind="asr")
    assert youtube.choose_track([]) is None
    assert youtube.choose_track([auto_en, manual_en]) is manual_en
    assert youtube.choose_track([auto_en, manual_fr], "fr") is manual_fr  # base-language match
    assert (
        youtube.choose_track([auto_de, auto_en], "de") is auto_de
    )  # only auto in original language
    assert youtube.choose_track([auto_de, manual_fr], "ja") is manual_fr  # no en/ja: any manual
    assert youtube.choose_track([auto_de]) is auto_de


def test_decide_verdicts() -> None:
    P = youtube.PlayerInfo
    assert youtube.decide(P("v", is_upcoming=True, is_live=True)) is youtube.Verdict.UPCOMING
    assert youtube.decide(P("v", is_live=True)) is youtube.Verdict.LIVE
    assert youtube.decide(P("v", playability_status="LOGIN_REQUIRED")) is youtube.Verdict.UNPLAYABLE
    assert youtube.decide(P("v", length_seconds=60)) is youtube.Verdict.SHORT
    assert youtube.decide(P("v", length_seconds=61)) is youtube.Verdict.OK
    assert youtube.decide(P("v", length_seconds=0)) is youtube.Verdict.OK


# --- innertube ---


async def test_innertube_key_outcomes(monkeypatch, sleeps) -> None:
    pages: list[tuple[int, str]] = []

    async def fake_get(url: str, headers=None):
        return pages.pop(0)

    monkeypatch.setattr(youtube, "_get_text", fake_get)
    pages.append((200, 'x "INNERTUBE_API_KEY": "AbC-_1" y'))
    assert await youtube._innertube_key("vid") == "AbC-_1"
    pages.append((404, ""))
    with pytest.raises(YouTubeError) as ei:
        await youtube._innertube_key("vid")
    assert ei.value.code is ErrorCode.NETWORK
    pages.append((200, '<a href="https://consent.youtube.com/m">'))
    with pytest.raises(YouTubeError) as ei:
        await youtube._innertube_key("vid")
    assert ei.value.code is ErrorCode.BLOCKED
    pages.append((200, "<div class='g-recaptcha'>"))
    with pytest.raises(YouTubeError) as ei:
        await youtube._innertube_key("vid")
    assert ei.value.code is ErrorCode.BLOCKED
    pages.append((200, "<html>no key</html>"))
    with pytest.raises(YouTubeError) as ei:
        await youtube._innertube_key("vid")
    assert ei.value.code is ErrorCode.PARSE_FAILED


async def test_fetch_player_info_success_error_and_bot_wall(monkeypatch, sleeps) -> None:
    async def key(video_id: str) -> str:
        return "K"

    posted: list[tuple[str, dict[str, Any]]] = []
    reply: list[tuple[int, dict[str, Any]]] = []

    async def post(url: str, payload: dict[str, Any], headers: dict[str, str]):
        posted.append((url, payload))
        return reply.pop(0)

    monkeypatch.setattr(youtube, "_innertube_key", key)
    monkeypatch.setattr(youtube, "_post_json", post)

    reply.append((200, _player()))
    info = await youtube.fetch_player_info("vid")
    assert info.title == "T"
    assert posted[0][0].endswith("/youtubei/v1/player?key=K") and posted[0][1]["videoId"] == "vid"

    reply.append((403, {}))
    with pytest.raises(YouTubeError) as ei:
        await youtube.fetch_player_info("vid")
    assert ei.value.code is ErrorCode.NETWORK

    bot = _player(
        playabilityStatus={"status": "LOGIN_REQUIRED", "reason": "Sign in: you are not a bot"}
    )
    reply.append((200, bot))
    with pytest.raises(YouTubeError) as ei:
        await youtube.fetch_player_info("vid")
    assert ei.value.code is ErrorCode.BLOCKED

    private = _player(playabilityStatus={"status": "UNPLAYABLE", "reason": "Private video"})
    reply.append((200, private))
    assert (await youtube.fetch_player_info("vid")).playability_reason == "Private video"


async def test_is_short_probe(monkeypatch, sleeps) -> None:
    status = {"v": 200}

    async def head(url: str) -> int:
        if status["v"] == -1:
            raise httpx.ConnectError("x")
        return status["v"]

    monkeypatch.setattr(youtube, "_head_status", head)
    assert await youtube.is_short("v", 30)  # short by length alone
    assert await youtube.is_short("v", 0)  # /shorts/ answers 200
    status["v"] = 303
    assert not await youtube.is_short("v", 120)
    status["v"] = -1
    assert not await youtube.is_short("v", 120)  # probe failure is non-fatal


# --- captions ---


def test_parse_caption_xml_formats() -> None:
    classic = '<transcript><text start="1.5" dur="2">Hello &amp;amp; welcome</text><text start="4" dur="1">[Music]</text><text start="5" dur="1">  </text></transcript>'  # noqa: E501
    segs = youtube.parse_caption_xml(classic)
    assert [(s.start, s.duration, s.text) for s in segs] == [(1.5, 2.0, "Hello & welcome")]
    srv3 = '<timedtext><body><p t="2000" d="1500"><s>First</s><s> line</s></p><p t="4000" d="500">♪</p></body></timedtext>'  # noqa: E501
    segs = youtube.parse_caption_xml(srv3)
    assert [(s.start, s.duration, s.text) for s in segs] == [(2.0, 1.5, "First line")]
    with pytest.raises(YouTubeError) as ei:
        youtube.parse_caption_xml("<not xml")
    assert ei.value.code is ErrorCode.PARSE_FAILED


def test_build_paragraphs_splits_on_sentence_end_pause_and_max() -> None:
    S = youtube.Segment
    sentence = "word " * 130  # > PARAGRAPH_MIN_CHARS once ended with a period
    segs = [
        S(0, 1, sentence.strip() + "."),
        S(1, 1, "second paragraph starts here"),
        S(100, 1, "after a long pause"),
    ]
    out = youtube.build_paragraphs(segs)
    assert out.split("\n\n")[0] == sentence.strip() + "."
    assert "second paragraph starts here after a long pause" in out
    big = [S(i, 1, "x" * 100) for i in range(30)]
    parts = youtube.build_paragraphs(big).split("\n\n")
    assert len(parts) > 1 and all(len(p) <= youtube.PARAGRAPH_MAX_CHARS + 101 for p in parts)
    assert youtube.build_paragraphs([]) == ""


def _fake_transcript_module(monkeypatch, fetch) -> None:
    mod = types.ModuleType("youtube_transcript_api")

    class Api:
        def fetch(self, video_id: str, languages: list[str]):
            return fetch(video_id, languages)

    mod.YouTubeTranscriptApi = Api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", mod)


def test_package_segments_fallback(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fetch(video_id: str, languages: list[str]):
        seen.append(languages)
        return [
            types.SimpleNamespace(start=1.0, duration=2.0, text="Real &amp; text"),
            types.SimpleNamespace(start=3.0, duration=1.0, text="[Applause]"),
        ]

    _fake_transcript_module(monkeypatch, fetch)
    segs = youtube._package_segments("vid", "fr")
    assert [(s.start, s.text) for s in segs] == [(1.0, "Real & text")]
    assert seen == [["fr", "en"]]
    youtube._package_segments("vid", None)
    assert seen[-1] == ["en"]

    def boom(video_id: str, languages: list[str]):
        raise RuntimeError("no transcript")

    _fake_transcript_module(monkeypatch, boom)
    with pytest.raises(YouTubeError) as ei:
        youtube._package_segments("vid", "fr")
    assert ei.value.code is ErrorCode.NO_CAPTIONS


async def test_download_segments_paths(monkeypatch, sleeps) -> None:
    info = youtube.PlayerInfo("vid")
    track = youtube.CaptionTrack("fr", "https://c/fr")
    xml = '<transcript><text start="0" dur="1">Bonjour</text></transcript>'
    reply: list[Any] = []

    async def fake_get(url: str, headers=None):
        r = reply.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    pkg_calls: list[tuple[str, str]] = []

    def fake_pkg(video_id: str, language: str | None):
        pkg_calls.append((video_id, language or ""))
        return [youtube.Segment(0, 1, "from package")]

    monkeypatch.setattr(youtube, "_get_text", fake_get)
    monkeypatch.setattr(youtube, "_package_segments", fake_pkg)

    reply.append((200, xml))
    assert (await youtube._download_segments(info, track))[0].text == "Bonjour"
    assert pkg_calls == []

    for unusable in [(200, "   "), (404, ""), (200, "<broken")]:
        reply.append(unusable)
        assert (await youtube._download_segments(info, track))[0].text == "from package"
    assert pkg_calls == [("vid", "fr")] * 3

    reply.append((429, ""))  # blocked is transient: no package fallback
    reply.extend([(429, "")] * len(youtube.RETRY_DELAYS_S))
    with pytest.raises(YouTubeError) as ei:
        await youtube._download_segments(info, track)
    assert ei.value.code is ErrorCode.BLOCKED
    assert len(pkg_calls) == 3


# --- fetch_video ---


def _info(**kw: Any) -> youtube.PlayerInfo:
    base: dict[str, Any] = dict(
        video_id="vid", length_seconds=600, description="  the description "
    )
    base.update(kw)
    return youtube.PlayerInfo(**base)


async def test_fetch_video_outcomes(monkeypatch) -> None:
    holder: dict[str, Any] = {"info": _info(), "segments": [], "short_probe": False}

    async def player(video_id: str):
        return holder["info"]

    async def short(video_id: str, length: int = 0) -> bool:
        return holder["short_probe"]

    async def download(info, track):
        if isinstance(holder["segments"], Exception):
            raise holder["segments"]
        return holder["segments"]

    monkeypatch.setattr(youtube, "fetch_player_info", player)
    monkeypatch.setattr(youtube, "is_short", short)
    monkeypatch.setattr(youtube, "_download_segments", download)

    holder["info"] = _info(is_upcoming=True)
    assert (await youtube.fetch_video("vid")).outcome is youtube.Outcome.UPCOMING
    holder["info"] = _info(is_live=True)
    assert (await youtube.fetch_video("vid")).outcome is youtube.Outcome.LIVE
    holder["info"] = _info(playability_status="ERROR", playability_reason="Video unavailable")
    res = await youtube.fetch_video("vid")
    assert res.outcome is youtube.Outcome.UNPLAYABLE and res.reason == "ERROR: Video unavailable"
    holder["info"] = _info(playability_status="ERROR", playability_reason="")
    assert (await youtube.fetch_video("vid")).reason == "ERROR"

    holder["info"] = _info(length_seconds=30)
    assert (await youtube.fetch_video("vid")).outcome is youtube.Outcome.SHORT
    holder["info"] = _info(length_seconds=0)
    holder["short_probe"] = True
    assert (await youtube.fetch_video("vid")).outcome is youtube.Outcome.SHORT
    holder["short_probe"] = False

    res = await youtube.fetch_video("vid")  # no caption tracks -> description
    assert (
        res.outcome is youtube.Outcome.NO_CAPTIONS
        and res.text == "the description"
        and res.track is None
    )

    track = youtube.CaptionTrack("en", "https://c/en")
    holder["info"] = _info(tracks=[track])
    holder["segments"] = [youtube.Segment(0, 1, "Hello there.")]
    res = await youtube.fetch_video("vid")
    assert (
        res.outcome is youtube.Outcome.TRANSCRIPT
        and res.text == "Hello there."
        and res.track is track
    )

    holder["segments"] = []
    res = await youtube.fetch_video("vid")
    assert res.outcome is youtube.Outcome.NO_CAPTIONS and res.reason == "caption track is empty"
    assert res.track is track

    holder["segments"] = YouTubeError(ErrorCode.NO_CAPTIONS)
    assert (await youtube.fetch_video("vid")).outcome is youtube.Outcome.NO_CAPTIONS
    holder["segments"] = YouTubeError(ErrorCode.NETWORK)
    with pytest.raises(YouTubeError):
        await youtube.fetch_video("vid")

    holder["info"] = _info(length_seconds=30, tracks=[track])
    holder["segments"] = [youtube.Segment(0, 1, "Clip text.")]
    assert (
        await youtube.fetch_video("vid", allow_short=True)
    ).outcome is youtube.Outcome.TRANSCRIPT


# --- oEmbed ---


async def test_fetch_oembed(monkeypatch) -> None:
    reply: list[Any] = []

    async def fake_get(url: str, headers=None):
        r = reply.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(youtube, "_get_text", fake_get)
    reply.append(
        (
            200,
            json.dumps(
                {"title": " Vid ", "author_name": "Chan", "thumbnail_url": "https://t/i.jpg"}
            ),
        )
    )
    assert await youtube.fetch_oembed(
        "https://www.youtube.com/watch?v=abcdefghijk"
    ) == youtube.OEmbed("Vid", "Chan", "https://t/i.jpg")
    reply.append((200, json.dumps({"title": "No extras"})))
    assert await youtube.fetch_oembed("u") == youtube.OEmbed("No extras", "", None)
    reply.append((200, json.dumps({"title": "  "})))
    assert await youtube.fetch_oembed("u") is None
    reply.append((404, ""))
    assert await youtube.fetch_oembed("u") is None
    reply.append((200, "not json"))
    assert await youtube.fetch_oembed("u") is None
    reply.append((200, "[1]"))  # list has no .get
    assert await youtube.fetch_oembed("u") is None
    reply.append(httpx.ConnectError("x"))
    assert await youtube.fetch_oembed("u") is None


# --- channels ---


def test_parse_channel_page() -> None:
    page = (
        '<meta property="og:title" content="Ch &amp; Co">'
        f'<link rel="alternate" type="application/rss+xml" href="https://www.youtube.com/feeds/videos.xml?channel_id={UC}">'
    )
    ch = youtube.parse_channel_page(page)
    assert ch is not None and (ch.channel_id, ch.title) == (UC, "Ch & Co")
    assert ch.site_url == f"https://www.youtube.com/channel/{UC}"
    assert ch.feed_url.endswith(UC)
    # alternate link without usable id falls back to embedded JSON
    noisy = (
        '<link rel="alternate" type="application/rss+xml" href="/x"><link rel="alternate" type="application/rss+xml">'  # noqa: E501
        + f'"externalId":"{UC}"'
    )
    assert youtube.parse_channel_page(noisy) == youtube.Channel(UC, "")
    assert youtube.parse_channel_page(f'"browseId": "{UC}"') == youtube.Channel(UC, "")
    assert youtube.parse_channel_page("<html></html>") is None


def test_channel_url_for_input() -> None:
    f = youtube._channel_url_for_input
    assert f("@handle") == "https://www.youtube.com/@handle"
    assert f("youtube.com/c/Name?x=1") == "https://www.youtube.com/c/Name"
    assert f("https://m.youtube.com/user/Bob") == "https://www.youtube.com/user/Bob"


async def test_resolve_channel_branches(monkeypatch, sleeps) -> None:
    assert await youtube.resolve_channel(f" {UC} ") == youtube.Channel(UC)
    feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={UC}"
    assert await youtube.resolve_channel(feed) == youtube.Channel(UC)
    assert await youtube.resolve_channel(
        f"https://www.youtube.com/channel/{UC}/videos"
    ) == youtube.Channel(UC)

    info = {"v": _info(channel_id=UC, channel="Name")}

    async def player(video_id: str):
        return info["v"]

    monkeypatch.setattr(youtube, "fetch_player_info", player)
    assert await youtube.resolve_channel("https://youtu.be/abcdefghijk") == youtube.Channel(
        UC, "Name"
    )
    info["v"] = _info(channel_id="", channel="x")
    with pytest.raises(YouTubeError, match="channel of the video"):
        await youtube.resolve_channel("https://youtu.be/abcdefghijk")

    pages: list[tuple[int, str]] = []
    fetched: list[str] = []

    async def fake_get(url: str, headers=None):
        fetched.append(url)
        return pages.pop(0)

    monkeypatch.setattr(youtube, "_get_text", fake_get)
    pages.append((200, f'"channelId":"{UC}"'))
    assert (await youtube.resolve_channel("@someone")).channel_id == UC
    assert fetched == ["https://www.youtube.com/@someone"]
    pages.append((404, ""))
    with pytest.raises(YouTubeError, match="channel not found"):
        await youtube.resolve_channel("@gone")
    pages.append((403, ""))
    with pytest.raises(YouTubeError) as ei:
        await youtube.resolve_channel("@forbidden")
    assert ei.value.code is ErrorCode.NETWORK
    pages.append((200, "<html>nothing</html>"))
    with pytest.raises(YouTubeError, match="no channel id"):
        await youtube.resolve_channel("@empty")


# --- takeout ---


def test_parse_takeout_csv_tolerant_and_strict() -> None:
    csv_bytes = (
        "\ufeffChannel Title,Channel ID,Channel URL\r\n"
        f'"Cool, Channel",{UC},http://www.youtube.com/channel/{UC}\r\n'
        f"Dup,{UC},x\r\n"
        "Bad,notanid,x\r\n"
        ",,\r\n"
        "Short\r\n"
    ).encode()
    rows = youtube.parse_takeout_csv(csv_bytes)
    assert rows.feeds == [("Cool, Channel", youtube.feed_url_for_channel(UC))]
    assert rows.invalid == 2  # bad id + truncated row
    assert youtube.parse_takeout_csv(b"").feeds == []
    only_ids = youtube.parse_takeout_csv(f"Channel ID\n{UC}\n".encode())
    assert only_ids.feeds == [("", youtube.feed_url_for_channel(UC))]
    with pytest.raises(ValueError, match="Channel ID"):
        youtube.parse_takeout_csv(b"a,b\n1,2\n")
