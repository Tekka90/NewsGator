"""YouTube support (YOUTUBE_PLAN.md) — the only module that knows about YouTube.

A YouTube channel is an ordinary `rss` feed (`videos.xml?channel_id=UC…`); a video is any
article whose canonical link is `https://www.youtube.com/watch?v=<id>`. Everything here is
derived from URLs — no schema, no stored state.

The transcript endpoints are unofficial: every network failure is mapped to a typed
`YouTubeError` and the pipeline never trusts a response shape (see `fulltext.py`).
HTTP calls go through the module-level seams `_get_text`, `_post_json` and `_head_status`
so tests never touch the network.
"""

import asyncio
import csv
import html
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import httpx

logger = logging.getLogger(__name__)

CANONICAL_BASE = "https://www.youtube.com/watch?v="
CHANNEL_FEED_BASE = "https://www.youtube.com/feeds/videos.xml?channel_id="
EMBED_BASE = "https://www.youtube-nocookie.com/embed/"

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE_RE = re.compile(r"^@[\w.\-]{1,100}$")

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_SHORT_HOST = "youtu.be"
_VIDEO_PATH_PREFIXES = ("shorts", "live", "embed", "v")

# Player recipe (innertube ANDROID client — same as youtube-transcript-api)
INNERTUBE_CLIENT_NAME = "ANDROID"
INNERTUBE_CLIENT_VERSION = "20.10.38"
ANDROID_USER_AGENT = f"com.google.android.youtube/{INNERTUBE_CLIENT_VERSION} (Linux; U; Android 14)"
WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# Skips the EU consent interstitial that otherwise replaces the page HTML.
_WEB_HEADERS = {"User-Agent": WEB_USER_AGENT, "Cookie": "SOCS=CAI", "Accept-Language": "en"}

SHORTS_MAX_SECONDS = 60
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 3.0)
HTTP_TIMEOUT_S = 20.0

# Paragraph shaping (plan 3.5)
PARAGRAPH_MIN_CHARS = 500
PARAGRAPH_MAX_CHARS = 800
PARAGRAPH_PAUSE_S = 2.0
_SENTENCE_END = (".", "!", "?", "…", "\u3002", "\uff01", "\uff1f")
_INLINE_CUE_RE = re.compile(r"\[[^\]]*\]")  # [Music], [Applause], [musique]… anywhere
_WHOLE_CUE_RE = re.compile(r"^(?:[(\uff08][^)\uff09]*[)\uff09]|[♪♫\s]+)$")  # "(applause)", "♪♪"


class ErrorCode(StrEnum):
    NO_CAPTIONS = "noCaptions"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    PARSE_FAILED = "parseFailed"
    NETWORK = "network"


class YouTubeError(Exception):
    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code
        self.message = message


# --- URLs -----------------------------------------------------------------------------


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_youtube_host(host: str) -> bool:
    host = host.lower()
    return host in _YOUTUBE_HOSTS or host == _SHORT_HOST


def parse_video_id(url: str) -> str | None:
    """Video id of a YouTube URL (watch, youtu.be, /shorts, /live, /embed, m./music.), else None.

    The host must be an exact YouTube host: `notyoutube.com` is not YouTube.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if not is_youtube_host(host):
        return None
    segments = [s for s in parsed.path.split("/") if s]
    candidate: str | None = None
    if host == _SHORT_HOST:
        candidate = segments[0] if segments else None
    elif segments and segments[0] == "watch":
        values = parse_qs(parsed.query).get("v")
        candidate = values[0] if values else None
    elif len(segments) >= 2 and segments[0] in _VIDEO_PATH_PREFIXES:
        candidate = segments[1]
    if candidate and _VIDEO_ID_RE.match(candidate):
        return candidate
    return None


def canonical_url(url: str) -> str | None:
    """`https://www.youtube.com/watch?v=<id>` for any YouTube video URL, else None."""
    video_id = parse_video_id(url)
    return f"{CANONICAL_BASE}{video_id}" if video_id else None


def is_video_url(url: str) -> bool:
    return parse_video_id(url) is not None


def video_id_for_link(link: str | None) -> str | None:
    return parse_video_id(link) if link else None


def media_kind_for_link(link: str | None) -> str | None:
    """`"video"` when the link is a YouTube video, else None (absent in the API)."""
    return "video" if video_id_for_link(link) else None


_OFFSET_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$")


def start_offset(url: str) -> int | None:
    """Player start offset in seconds from `t` / `start` (`90`, `90s`, `1m30s`), else None."""
    query = parse_qs(urlparse(url).query)
    raw = (query.get("t") or query.get("start") or [""])[0].strip().lower()
    match = _OFFSET_RE.match(raw) if raw else None
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def embed_url(video_id: str, start: int | None = None) -> str:
    return f"{EMBED_BASE}{video_id}" + (f"?start={start}" if start else "")


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def is_channel_feed_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        (parsed.hostname or "").lower() in _YOUTUBE_HOSTS
        and parsed.path == "/feeds/videos.xml"
        and bool(parse_qs(parsed.query).get("channel_id"))
    )


def is_channel_input(raw: str) -> bool:
    """True for anything the add-feed flow should resolve as a YouTube channel/video:
    a bare `UC…` id, a bare `@handle`, or a URL on a YouTube host."""
    value = (raw or "").strip()
    if _CHANNEL_ID_RE.match(value) or _HANDLE_RE.match(value):
        return True
    return is_youtube_host(_host(value if "://" in value else f"https://{value}")) and bool(value)


def feed_url_for_channel(channel_id: str) -> str:
    return f"{CHANNEL_FEED_BASE}{channel_id}"


def channel_id_from_feed_url(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("channel_id")
    if values and _CHANNEL_ID_RE.match(values[0]):
        return values[0]
    return None


# --- HTTP seams -----------------------------------------------------------------------


async def _get_text(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT_S) as client:
        resp = await client.get(url, headers=headers or _WEB_HEADERS)
    return resp.status_code, resp.text


async def _post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        resp = await client.post(url, json=payload, headers=headers)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, data if isinstance(data, dict) else {}


async def _head_status(url: str) -> int:
    """Status of a HEAD request WITHOUT following redirects."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=HTTP_TIMEOUT_S) as client:
        resp = await client.head(url, headers=_WEB_HEADERS)
    return resp.status_code


async def _retrying(call: Any, *args: Any) -> Any:
    """Run an HTTP seam with the existing backoff on transient failures (network, 429, 5xx).

    Raises YouTubeError(network) / (blocked) when the attempts are exhausted.
    """
    last: YouTubeError | None = None
    for attempt in range(len(RETRY_DELAYS_S) + 1):
        try:
            result = await call(*args)
        except httpx.HTTPError as exc:
            last = YouTubeError(ErrorCode.NETWORK, f"{type(exc).__name__}: {exc}"[:200])
        else:
            status = result[0] if isinstance(result, tuple) else 200
            if status == 429:
                last = YouTubeError(ErrorCode.BLOCKED, "HTTP 429 (too many requests)")
            elif status >= 500:
                last = YouTubeError(ErrorCode.NETWORK, f"HTTP {status}")
            else:
                return result
        if attempt < len(RETRY_DELAYS_S):
            await asyncio.sleep(RETRY_DELAYS_S[attempt])
    assert last is not None
    raise last


# --- player ---------------------------------------------------------------------------


@dataclass
class CaptionTrack:
    language_code: str
    base_url: str
    kind: str | None = None  # "asr" for auto-generated, None for manual
    name: str = ""

    @property
    def is_auto(self) -> bool:
        return self.kind == "asr"


@dataclass
class PlayerInfo:
    video_id: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""
    description: str = ""
    length_seconds: int = 0
    is_live: bool = False
    is_upcoming: bool = False
    is_live_content: bool = False
    playability_status: str = "OK"
    playability_reason: str = ""
    tracks: list[CaptionTrack] = field(default_factory=list)
    original_language: str | None = None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text_of(node: Any) -> str:
    """Text of an innertube text node (`simpleText` or `runs`)."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if isinstance(node.get("simpleText"), str):
            return str(node["simpleText"])
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
    return ""


def _base_language(code: str) -> str:
    return code.lower().replace("_", "-").split("-")[0]


def _original_language(renderer: dict[str, Any], tracks: list[CaptionTrack]) -> str | None:
    """Plan 3.6.1: language of the caption track the default audio track points to."""
    audio_tracks = renderer.get("audioTracks")
    if not isinstance(audio_tracks, list) or not audio_tracks:
        return None
    index = _as_int(renderer.get("defaultAudioTrackIndex"))
    if not 0 <= index < len(audio_tracks) or not isinstance(audio_tracks[index], dict):
        index = 0
    audio = audio_tracks[index]
    indices = audio.get("captionTrackIndices")
    if isinstance(indices, list) and indices:
        position = _as_int(indices[0])
        if 0 <= position < len(tracks):
            return tracks[position].language_code
    return None


def parse_player_response(video_id: str, data: dict[str, Any]) -> PlayerInfo:
    """Extract everything we need from an innertube `player` response (shape-tolerant)."""
    details = data.get("videoDetails")
    details = details if isinstance(details, dict) else {}
    playability = data.get("playabilityStatus")
    playability = playability if isinstance(playability, dict) else {}
    info = PlayerInfo(
        video_id=video_id,
        title=str(details.get("title") or ""),
        channel=str(details.get("author") or ""),
        channel_id=str(details.get("channelId") or ""),
        description=str(details.get("shortDescription") or ""),
        length_seconds=_as_int(details.get("lengthSeconds")),
        is_live=bool(details.get("isLive")),
        is_upcoming=bool(details.get("isUpcoming")),
        is_live_content=bool(details.get("isLiveContent")),
        playability_status=str(playability.get("status") or "OK"),
        playability_reason=str(playability.get("reason") or ""),
    )
    captions = data.get("captions")
    renderer = captions.get("playerCaptionsTracklistRenderer") if isinstance(captions, dict) else {}
    renderer = renderer if isinstance(renderer, dict) else {}
    for raw in renderer.get("captionTracks") or []:
        if not isinstance(raw, dict) or not raw.get("baseUrl"):
            continue
        kind = raw.get("kind")
        info.tracks.append(
            CaptionTrack(
                language_code=str(raw.get("languageCode") or ""),
                base_url=str(raw["baseUrl"]),
                kind=str(kind) if kind else None,
                name=_text_of(raw.get("name")),
            )
        )
    info.original_language = _original_language(renderer, info.tracks)
    return info


def choose_track(
    tracks: list[CaptionTrack], original_language: str | None = None
) -> CaptionTrack | None:
    """Caption choice (plan 3.6), identical on both sides.

    1. original language (default audio track → else `en`),
    2. manual before auto-generated within that language,
    3. otherwise any manual track, then any auto track.
    """
    if not tracks:
        return None

    def of_language(code: str) -> list[CaptionTrack]:
        exact = [t for t in tracks if t.language_code.lower() == code.lower()]
        return exact or [
            t for t in tracks if _base_language(t.language_code) == _base_language(code)
        ]

    for language in [original_language, "en"]:
        if not language:
            continue
        candidates = of_language(language)
        manual = [t for t in candidates if not t.is_auto]
        if manual:
            return manual[0]
        if candidates:
            return candidates[0]
    manual_any = [t for t in tracks if not t.is_auto]
    return manual_any[0] if manual_any else tracks[0]


class Verdict(StrEnum):
    OK = "ok"
    SHORT = "short"
    LIVE = "live"
    UPCOMING = "upcoming"
    UNPLAYABLE = "unplayable"


def decide(info: PlayerInfo) -> Verdict:
    """Filtering (plan 3.8) from the player response alone; the Shorts HEAD probe is separate."""
    if info.is_upcoming:
        return Verdict.UPCOMING
    if info.is_live:
        return Verdict.LIVE
    if info.playability_status.upper() != "OK":
        return Verdict.UNPLAYABLE
    if 0 < info.length_seconds <= SHORTS_MAX_SECONDS:
        return Verdict.SHORT
    return Verdict.OK


_BOT_MARKERS = ("not a bot", "unusual traffic", "automated queries")


async def _innertube_key(video_id: str) -> str:
    status, page = await _retrying(_get_text, f"https://www.youtube.com/watch?v={video_id}")
    if status != 200:
        raise YouTubeError(ErrorCode.NETWORK, f"watch page HTTP {status}")
    if "consent.youtube.com" in page[:2000] or "g-recaptcha" in page:
        raise YouTubeError(ErrorCode.BLOCKED, "consent or captcha page")
    match = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([A-Za-z0-9_-]+)"', page)
    if not match:
        raise YouTubeError(ErrorCode.PARSE_FAILED, "INNERTUBE_API_KEY not found")
    return match.group(1)


async def fetch_player_info(video_id: str) -> PlayerInfo:
    """Title, channel, duration, live flags and caption tracks via the innertube player call."""
    api_key = await _innertube_key(video_id)
    payload = {
        "context": {
            "client": {
                "clientName": INNERTUBE_CLIENT_NAME,
                "clientVersion": INNERTUBE_CLIENT_VERSION,
                "androidSdkVersion": 34,
                "hl": "en",
            }
        },
        "videoId": video_id,
    }
    headers = {"User-Agent": ANDROID_USER_AGENT, "Content-Type": "application/json"}
    status, data = await _retrying(
        _post_json,
        f"https://www.youtube.com/youtubei/v1/player?key={api_key}",
        payload,
        headers,
    )
    if status != 200 or not data:
        raise YouTubeError(ErrorCode.NETWORK, f"player HTTP {status}")
    info = parse_player_response(video_id, data)
    reason = info.playability_reason.lower()
    if info.playability_status.upper() != "OK" and any(m in reason for m in _BOT_MARKERS):
        raise YouTubeError(ErrorCode.BLOCKED, info.playability_reason[:200])
    return info


async def is_short(video_id: str, length_seconds: int = 0) -> bool:
    """Shorts: `lengthSeconds <= 60`, or `/shorts/<id>` answers 200 without redirect."""
    if 0 < length_seconds <= SHORTS_MAX_SECONDS:
        return True
    try:
        status = await _retrying(_head_status, f"https://www.youtube.com/shorts/{video_id}")
    except (YouTubeError, httpx.HTTPError):
        return False  # probe failure is non-fatal: treat as a normal video
    return bool(status == 200)


# --- transcript -----------------------------------------------------------------------


@dataclass
class Segment:
    start: float
    duration: float
    text: str


def _clean_segment_text(raw: str) -> str:
    # The XML parser decoded the entities once; caption text is escaped twice.
    unescaped = html.unescape(raw).replace("\u200b", "")
    return " ".join(_INLINE_CUE_RE.sub(" ", unescaped).split())


def _is_cue(text: str) -> bool:
    return not text or bool(_WHOLE_CUE_RE.match(text))


def parse_caption_xml(xml_text: str) -> list[Segment]:
    """Segments from a caption document: `<transcript><text start dur>` or srv3 `<timedtext>`."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise YouTubeError(ErrorCode.PARSE_FAILED, f"caption XML: {exc}") from exc
    segments: list[Segment] = []
    for el in root.iter("text"):
        text = _clean_segment_text("".join(el.itertext()))
        if _is_cue(text):
            continue
        segments.append(Segment(float(el.get("start") or 0), float(el.get("dur") or 0), text))
    if not segments:
        for el in root.iter("p"):  # srv3: times in milliseconds, text in <s> children
            text = _clean_segment_text("".join(el.itertext()))
            if _is_cue(text):
                continue
            segments.append(Segment(_as_int(el.get("t")) / 1000, _as_int(el.get("d")) / 1000, text))
    return segments


def build_paragraphs(segments: list[Segment]) -> str:
    """Join segments into paragraphs of roughly 500-800 characters (plan 3.5).

    A paragraph closes once it has PARAGRAPH_MIN_CHARS and the segment ends a sentence or is
    followed by a pause > PARAGRAPH_PAUSE_S; it always closes at PARAGRAPH_MAX_CHARS. Words are
    never split. Paragraphs are separated by a blank line; no timestamps.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    length = 0
    for i, seg in enumerate(segments):
        current.append(seg.text)
        length += len(seg.text) + 1
        following = segments[i + 1] if i + 1 < len(segments) else None
        pause = following is not None and following.start - (seg.start + seg.duration) > (
            PARAGRAPH_PAUSE_S
        )
        boundary = seg.text.rstrip().endswith(_SENTENCE_END) or pause
        if length >= PARAGRAPH_MAX_CHARS or (length >= PARAGRAPH_MIN_CHARS and boundary):
            paragraphs.append(" ".join(current))
            current, length = [], 0
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _package_segments(video_id: str, language: str | None) -> list[Segment]:
    """Blocking fallback through `youtube-transcript-api` (used when our own caption
    download comes back empty, e.g. a PO-token gated base URL)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    languages = [language] if language else []
    languages.append("en")
    try:
        fetched = api.fetch(video_id, languages=languages)
    except Exception as exc:
        raise YouTubeError(ErrorCode.NO_CAPTIONS, f"{type(exc).__name__}"[:120]) from exc
    return [
        Segment(s.start, s.duration, text)
        for s in fetched
        if not _is_cue(text := _clean_segment_text(s.text))
    ]


async def _download_segments(info: PlayerInfo, track: CaptionTrack) -> list[Segment]:
    url = track.base_url
    try:
        status, body = await _retrying(_get_text, url, {"User-Agent": ANDROID_USER_AGENT})
        if status == 200 and body.strip():
            segments = parse_caption_xml(body)
            if segments:
                return segments
    except YouTubeError as exc:
        if exc.code in (ErrorCode.BLOCKED, ErrorCode.NETWORK):
            raise
        logger.info("caption XML for %s unusable (%s); trying the package", info.video_id, exc)
    return await anyio.to_thread.run_sync(_package_segments, info.video_id, track.language_code)


class Outcome(StrEnum):
    TRANSCRIPT = "transcript"
    NO_CAPTIONS = "no_captions"  # text is the video description
    SHORT = "short"
    LIVE = "live"
    UPCOMING = "upcoming"
    UNPLAYABLE = "unplayable"


@dataclass
class VideoResult:
    outcome: Outcome
    info: PlayerInfo
    text: str = ""
    track: CaptionTrack | None = None
    reason: str = ""


async def fetch_video(video_id: str) -> VideoResult:
    """Player info + filtering + transcript for one video.

    Returns an `Outcome` for every expected situation; raises YouTubeError(network|blocked|
    parseFailed) only for transient/unknown failures the caller should retry later.
    """
    info = await fetch_player_info(video_id)
    verdict = decide(info)
    if verdict is Verdict.UPCOMING:
        return VideoResult(Outcome.UPCOMING, info, reason="premiere or live stream not started")
    if verdict is Verdict.LIVE:
        return VideoResult(Outcome.LIVE, info, reason="live stream in progress")
    if verdict is Verdict.UNPLAYABLE:
        reason = f"{info.playability_status}: {info.playability_reason}".strip(": ")
        return VideoResult(Outcome.UNPLAYABLE, info, reason=reason[:300])
    if verdict is Verdict.SHORT or await is_short(video_id, info.length_seconds):
        return VideoResult(Outcome.SHORT, info, reason="short-form video")

    track = choose_track(info.tracks, info.original_language)
    if track is None:
        return VideoResult(
            Outcome.NO_CAPTIONS,
            info,
            text=info.description.strip(),
            reason="no captions available",
        )
    try:
        segments = await _download_segments(info, track)
    except YouTubeError as exc:
        if exc.code is not ErrorCode.NO_CAPTIONS:
            raise
        segments = []
    text = build_paragraphs(segments)
    if not text:
        return VideoResult(
            Outcome.NO_CAPTIONS,
            info,
            text=info.description.strip(),
            track=track,
            reason="caption track is empty",
        )
    return VideoResult(Outcome.TRANSCRIPT, info, text=text, track=track)


# --- oEmbed ---------------------------------------------------------------------------


@dataclass
class OEmbed:
    title: str
    author_name: str
    thumbnail_url: str | None


async def fetch_oembed(canonical: str) -> OEmbed | None:
    """Title/channel/thumbnail for a shared link. Failure is non-fatal (returns None)."""
    url = f"https://www.youtube.com/oembed?url={canonical}&format=json"
    try:
        status, body = await _get_text(url, {"User-Agent": WEB_USER_AGENT})  # single attempt
        if status != 200:
            return None
        data = json.loads(body)
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        thumb = data.get("thumbnail_url")
        return OEmbed(title, str(data.get("author_name") or ""), str(thumb) if thumb else None)
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


# --- channel resolution ---------------------------------------------------------------


@dataclass
class Channel:
    channel_id: str
    title: str = ""

    @property
    def feed_url(self) -> str:
        return feed_url_for_channel(self.channel_id)

    @property
    def site_url(self) -> str:
        return f"https://www.youtube.com/channel/{self.channel_id}"


_RSS_LINK_RE = re.compile(r"<link[^>]+>", re.IGNORECASE)
_CHANNEL_JSON_RES = (
    re.compile(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"browseId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
)
_META_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"', re.IGNORECASE)


def parse_channel_page(page: str) -> Channel | None:
    """Channel from a channel page: the RSS `<link rel=alternate>`, else embedded JSON ids."""
    title_match = _META_TITLE_RE.search(page)
    title = html.unescape(title_match.group(1)) if title_match else ""
    for tag in _RSS_LINK_RE.findall(page):
        if "alternate" in tag and "rss+xml" in tag:
            href = re.search(r'href="([^"]+)"', tag)
            if href:
                channel_id = channel_id_from_feed_url(html.unescape(href.group(1)))
                if channel_id:
                    return Channel(channel_id, title)
    for pattern in _CHANNEL_JSON_RES:
        found = pattern.search(page)
        if found:
            return Channel(found.group(1), title)
    return None


def _channel_url_for_input(raw: str) -> str:
    value = raw.strip()
    if _HANDLE_RE.match(value):
        return f"https://www.youtube.com/{value}"
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    return f"https://www.youtube.com{parsed.path}"


async def resolve_channel(raw: str) -> Channel:
    """Resolve a channel URL (`/@handle`, `/channel/UC…`, `/c/…`, `/user/…`), a video URL, a
    channel feed URL or a bare `UC…` id to its channel. Raises YouTubeError(unavailable)."""
    value = raw.strip()
    if _CHANNEL_ID_RE.match(value):
        return Channel(value)
    if is_channel_feed_url(value):
        channel_id = channel_id_from_feed_url(value)
        if channel_id:
            return Channel(channel_id)
    video_id = parse_video_id(value)
    if video_id:
        info = await fetch_player_info(video_id)
        if _CHANNEL_ID_RE.match(info.channel_id):
            return Channel(info.channel_id, info.channel)
        raise YouTubeError(ErrorCode.UNAVAILABLE, "channel of the video not found")
    segments = [
        s for s in urlparse(value if "://" in value else f"https://{value}").path.split("/") if s
    ]
    if len(segments) >= 2 and segments[0] == "channel" and _CHANNEL_ID_RE.match(segments[1]):
        return Channel(segments[1])
    status, page = await _retrying(_get_text, _channel_url_for_input(value))
    if status == 404:
        raise YouTubeError(ErrorCode.UNAVAILABLE, "channel not found")
    if status != 200:
        raise YouTubeError(ErrorCode.NETWORK, f"channel page HTTP {status}")
    channel = parse_channel_page(page)
    if channel is None:
        raise YouTubeError(ErrorCode.UNAVAILABLE, "no channel id on the page")
    return channel


# --- Takeout subscriptions ------------------------------------------------------------


@dataclass
class TakeoutRows:
    feeds: list[tuple[str, str]] = field(default_factory=list)  # (title, rss url)
    invalid: int = 0


def parse_takeout_csv(content: bytes) -> TakeoutRows:
    """Google Takeout `subscriptions.csv` (`Channel ID,Channel URL,Channel title`).

    Tolerates a UTF-8 BOM, CRLF line ends, quoted titles and reordered columns; rows without
    a valid `UC…` id count as invalid; duplicate ids are collapsed.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    result = TakeoutRows()
    if not rows:
        return result
    header = [cell.strip().lower() for cell in rows[0]]
    if "channel id" not in header:
        raise ValueError("Not a subscriptions.csv file (missing 'Channel ID' column)")
    id_col = header.index("channel id")
    title_col = header.index("channel title") if "channel title" in header else None
    seen: set[str] = set()
    for row in rows[1:]:
        channel_id = row[id_col].strip() if id_col < len(row) else ""
        if not _CHANNEL_ID_RE.match(channel_id):
            result.invalid += 1
            continue
        if channel_id in seen:
            continue
        seen.add(channel_id)
        title = row[title_col].strip() if title_col is not None and title_col < len(row) else ""
        result.feeds.append((title, feed_url_for_channel(channel_id)))
    return result
