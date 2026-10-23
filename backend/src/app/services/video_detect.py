"""Video detection on article pages (VIDEO_ARTICLES_PLAN §4) — pure functions, no I/O.

An article whose main content is a video (France 24 clips, Vimeo/Dailymotion pages, native
``<video>`` players, YouTube embeds) is flagged exactly like a native YouTube feed item:
``media_kind = "video"`` plus ``video_url`` / ``video_embed_url`` / ``video_provider`` /
``video_id`` / ``video_duration`` persisted on the article.

Detection runs once on the HTML the full-text chain already fetched. Signals, ranked, first
usable hit wins:

1. JSON-LD ``VideoObject`` (top level, ``@graph``, or nested ``video`` of an article);
2. ``og:video*``, ``twitter:player*``, ``og:type`` starting with ``video.``;
3. in the page content: ``<video>`` or an ``<iframe>`` of YouTube / Vimeo / Dailymotion.

D10: the hit only flags the article when the video *is* the main content — the page is a video
(JSON-LD ``VideoObject`` at page level or ``og:type=video.*``), or the extracted body is shorter
than ``VIDEO_MAIN_CONTENT_MAX_CHARS``, or the video appears before the first body paragraph.

The Swift port (``NewsGatorCore/VideoDetector``) mirrors this module line for line and runs the
same fixtures (``tests/fixtures/video_pages`` + ``video_detect_vectors.json``). Detection never
raises; only ``http(s)`` URLs are kept and nothing is fetched here (no SSRF surface).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from app.services import youtube

MEDIA_KIND_VIDEO = "video"
PROVIDER_YOUTUBE = "youtube"
PROVIDER_FILE = "file"
PROVIDER_EMBED = "embed"

# D10: below this many extracted body characters, a page with a video is "a video page".
VIDEO_MAIN_CONTENT_MAX_CHARS = 1500
# A paragraph shorter than this does not count as the start of the article body.
MIN_BODY_PARAGRAPH_CHARS = 80
_MAX_HTML_CHARS = 2_000_000
_MAX_JSONLD_DEPTH = 6
_SOURCE_LOOKAHEAD_CHARS = 2000

_MEDIA_EXTENSIONS = (".mp4", ".webm", ".m3u8", ".mov")
_HLS_MIMES = ("application/vnd.apple.mpegurl", "application/x-mpegurl")
_EMBED_HOST_SUFFIXES = ("vimeo.com", "dailymotion.com")
_EMBED_HOSTS = ("dai.ly",)

_ATTR_RE = re.compile(r"""([^\s"'<>/=]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_TAG_RE = re.compile(r"<(meta|video|source|iframe)\b([^>]*)>", re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_JSONLD_RE = re.compile(
    r"<script[^>]+type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")
_DURATION_RE = re.compile(
    r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.\d+)?S)?)?$"
)


@dataclass(frozen=True)
class VideoHint:
    """A detected video. ``provider`` is ``youtube`` / ``file`` / ``embed`` (player tier, D12)."""

    url: str
    provider: str
    embed_url: str | None = None
    youtube_id: str | None = None
    duration_s: int | None = None
    description: str | None = None


@dataclass(frozen=True)
class VideoCandidate:
    """A hit before the main-content decision (D10)."""

    hint: VideoHint
    page_is_video: bool
    precedes_body: bool


@dataclass(frozen=True)
class _Raw:
    content_url: str | None = None
    embed_url: str | None = None
    mime: str | None = None
    duration: str | None = None
    description: str | None = None
    assume_file: bool = False


def parse_iso_duration(value: object) -> int | None:
    """ISO 8601 duration (``PT52S``, ``P0DT0H0M52S``, ``PT1H2M3S``) → whole seconds, else None."""
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip().upper())
    if not match or not any(g is not None for g in match.groups()):
        return None
    weeks, days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return ((weeks * 7 + days) * 24 + hours) * 3600 + minutes * 60 + seconds


def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        value = match.group(2) or match.group(3) or match.group(4) or ""
        out.setdefault(match.group(1).lower(), html_lib.unescape(value).strip())
    return out


def _clean_url(raw: object, base_url: str) -> str | None:
    """Absolute http(s) URL or None (relative URLs resolved against the page URL)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if value.startswith("//"):
        value = "https:" + value
    elif base_url:
        value = urljoin(base_url, value)
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    return value


def _is_direct_media(url: str, mime: str | None) -> bool:
    kind = (mime or "").strip().lower()
    if kind.startswith("video/") or kind in _HLS_MIMES:
        return True
    return urlparse(url).path.lower().endswith(_MEDIA_EXTENSIONS)


def _is_embed_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _EMBED_HOSTS or any(
        host == s or host.endswith("." + s) for s in _EMBED_HOST_SUFFIXES
    )


def _hint_from(raw: _Raw, base_url: str) -> VideoHint | None:
    content = _clean_url(raw.content_url, base_url)
    embed = _clean_url(raw.embed_url, base_url)
    description = raw.description.strip() if isinstance(raw.description, str) else None
    duration = parse_iso_duration(raw.duration)
    extras: dict[str, Any] = {"duration_s": duration, "description": description or None}
    for candidate in (content, embed):
        video_id = youtube.parse_video_id(candidate) if candidate else None
        if video_id:
            return VideoHint(
                url=f"{youtube.CANONICAL_BASE}{video_id}",
                provider=PROVIDER_YOUTUBE,
                embed_url=youtube.embed_url(video_id),
                youtube_id=video_id,
                **extras,
            )
    for candidate, mime in ((content, raw.mime), (embed, None)):
        if candidate and (_is_direct_media(candidate, mime) or (raw.assume_file and candidate)):
            other = embed if candidate == content else content
            return VideoHint(
                url=candidate,
                provider=PROVIDER_FILE,
                embed_url=other if other and other != candidate else None,
                **extras,
            )
    main = content or embed
    if main is None:
        return None
    return VideoHint(url=main, provider=PROVIDER_EMBED, embed_url=embed, **extras)


# --- signal 1: JSON-LD ----------------------------------------------------------------


def _types(node: dict[str, Any]) -> list[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def _collect_video_objects(
    node: Any, nested: bool, depth: int, out: list[tuple[dict[str, Any], bool]]
) -> None:
    """VideoObject nodes with a flag: True when found under a ``video`` property of another node."""
    if depth > _MAX_JSONLD_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            _collect_video_objects(item, nested, depth + 1, out)
        return
    if not isinstance(node, dict):
        return
    if "VideoObject" in _types(node):
        out.append((node, nested))
    _collect_video_objects(node.get("@graph"), nested, depth + 1, out)
    if "VideoObject" not in _types(node):
        _collect_video_objects(node.get("video"), True, depth + 1, out)


def _jsonld_hint(html: str, base_url: str) -> tuple[VideoHint, bool] | None:
    found: list[tuple[dict[str, Any], bool]] = []
    for block in _JSONLD_RE.findall(html):
        try:
            _collect_video_objects(json.loads(block), False, 0, found)
        except (ValueError, TypeError, RecursionError):
            continue
    for want_nested in (False, True):  # page-level objects outrank an article's nested video
        for node, nested in found:
            if nested is not want_nested:
                continue
            mime = node.get("encodingFormat")
            hint = _hint_from(
                _Raw(
                    content_url=node.get("contentUrl"),
                    embed_url=node.get("embedUrl"),
                    mime=mime if isinstance(mime, str) else None,
                    duration=node.get("duration"),
                    description=node.get("description"),
                ),
                base_url,
            )
            if hint:
                return hint, not nested
    return None


# --- signal 2: meta tags --------------------------------------------------------------


def _meta_map(tags: list[tuple[str, dict[str, str]]]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for name, attrs in tags:
        if name != "meta":
            continue
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "")
        if key and content:
            meta.setdefault(key, content)
    return meta


def _meta_hint(meta: dict[str, str], base_url: str) -> VideoHint | None:
    og_url = next(
        (meta[k] for k in ("og:video:secure_url", "og:video:url", "og:video") if k in meta), None
    )
    player = meta.get("twitter:player")
    stream = meta.get("twitter:player:stream")
    if og_url:
        raw = _Raw(content_url=og_url, embed_url=player, mime=meta.get("og:video:type"))
    elif stream:
        raw = _Raw(
            content_url=stream,
            embed_url=player,
            mime=meta.get("twitter:player:stream:content_type"),
        )
    elif player:
        raw = _Raw(embed_url=player)
    else:
        return None
    return _hint_from(raw, base_url)


# --- signal 3: page content -----------------------------------------------------------


def _content_hint(
    cleaned: str, base_url: str
) -> tuple[VideoHint, int] | None:
    """First ``<video>`` / provider ``<iframe>`` of the content, with its offset."""
    for match in _TAG_RE.finditer(cleaned):
        name, attrs = match.group(1).lower(), _attrs(match.group(2))
        if name == "iframe":
            src = attrs.get("src") or attrs.get("data-src")
            url = _clean_url(src, base_url)
            if url and (youtube.parse_video_id(url) or _is_embed_host(url)):
                hint = _hint_from(_Raw(embed_url=url), base_url)
                if hint:
                    return hint, match.start()
        elif name == "video":
            src = attrs.get("src")
            mime = attrs.get("type")
            if not _clean_url(src, base_url):
                tail = cleaned[match.end(): match.end() + _SOURCE_LOOKAHEAD_CHARS]
                tail = tail.split("</video", 1)[0]
                for source in re.finditer(r"<source\b([^>]*)>", tail, re.IGNORECASE):
                    s_attrs = _attrs(source.group(1))
                    if _clean_url(s_attrs.get("src"), base_url):
                        src, mime = s_attrs.get("src"), s_attrs.get("type")
                        break
            hint = _hint_from(_Raw(content_url=src, mime=mime, assume_file=True), base_url)
            if hint:
                return hint, match.start()
    return None


def _precedes_body(cleaned: str, offset: int) -> bool:
    """True when the video sits before the first substantial paragraph (or there is none)."""
    for match in _PARAGRAPH_RE.finditer(cleaned):
        text = " ".join(html_lib.unescape(_STRIP_TAGS_RE.sub(" ", match.group(1))).split())
        if len(text) >= MIN_BODY_PARAGRAPH_CHARS:
            return offset < match.start()
    return True


# --- public API -----------------------------------------------------------------------


def find_video(html: str, base_url: str = "") -> VideoCandidate | None:
    """Best video of a page with the signals needed for the main-content decision."""
    html = (html or "")[:_MAX_HTML_CHARS]
    cleaned = _SCRIPT_STYLE_RE.sub("", _COMMENT_RE.sub("", html))
    tags = [(m.group(1).lower(), _attrs(m.group(2))) for m in _TAG_RE.finditer(cleaned)]
    meta = _meta_map(tags)
    og_is_video = meta.get("og:type", "").lower().startswith("video.")

    from_jsonld = _jsonld_hint(html, base_url)
    if from_jsonld:
        hint, page_level = from_jsonld
        return VideoCandidate(hint, page_level or og_is_video, False)
    from_meta = _meta_hint(meta, base_url)
    if from_meta:
        return VideoCandidate(from_meta, og_is_video, False)
    from_content = _content_hint(cleaned, base_url)
    if from_content:
        hint, offset = from_content
        return VideoCandidate(hint, og_is_video, _precedes_body(cleaned, offset))
    return None


def is_main_content(candidate: VideoCandidate, body_chars: int) -> bool:
    """D10: the video is what the page is about."""
    return (
        candidate.page_is_video
        or body_chars < VIDEO_MAIN_CONTENT_MAX_CHARS
        or candidate.precedes_body
    )


def detect_video(html: str, base_url: str = "", body_chars: int = 0) -> VideoHint | None:
    """HTML → ``VideoHint`` when the page's main content is a video, else None. Never raises."""
    try:
        candidate = find_video(html, base_url)
        if candidate and is_main_content(candidate, body_chars):
            return candidate.hint
    except Exception:
        return None
    return None


def hint_for_youtube_url(url: str) -> VideoHint | None:
    """The hint of a native YouTube video link (feed item, shared link)."""
    video_id = youtube.parse_video_id(url or "")
    if not video_id:
        return None
    return VideoHint(
        url=f"{youtube.CANONICAL_BASE}{video_id}",
        provider=PROVIDER_YOUTUBE,
        embed_url=youtube.embed_url(video_id),
        youtube_id=video_id,
    )


def video_fields(hint: VideoHint | None) -> dict[str, Any]:
    """Article column values of a hint (empty for no hint) — one code path for both sources."""
    if hint is None:
        return {}
    return {
        "media_kind": MEDIA_KIND_VIDEO,
        "video_url": hint.url,
        "video_embed_url": hint.embed_url,
        "video_provider": hint.provider,
        "video_id": hint.youtube_id,
        "video_duration": hint.duration_s,
    }


def youtube_video_fields(url: str) -> dict[str, Any]:
    """Columns for a native YouTube link at article creation (empty when not a video)."""
    return video_fields(hint_for_youtube_url(url))
