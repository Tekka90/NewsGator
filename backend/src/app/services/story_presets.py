"""Story presets ("Smart Views"): defaults, validation, criteria evaluation.

A preset is a filter plus a presentation (sort key/direction, icon, label, display mode).
Presets are stored per user as one JSON document (``user.story_presets``; NULL = defaults,
computed, not persisted). The pure functions here are the reference semantics shared with
the Apple clients through ``tests/fixtures/story_presets_vectors.json``.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

BUILTIN_IDS: tuple[str, ...] = ("all", "unread", "updated", "saved")
STATUSES: tuple[str, ...] = ("any", "unread", "updated", "saved")
SOURCE_KINDS: tuple[str, ...] = ("rss", "mail", "youtube", "saved", "reader_api")
SORTS: tuple[str, ...] = ("published", "updated", "sources", "saved_at")
ORDERS: tuple[str, ...] = ("asc", "desc")
DISPLAYS: tuple[str, ...] = ("icon+label", "icon", "label")
KINDS: tuple[str, ...] = ("builtin", "custom")

ICON_CATALOG: tuple[str, ...] = (
    "list", "circle-dot", "refresh", "bookmark", "star", "heart", "flag", "tag", "folder",
    "inbox", "clock", "calendar", "flame", "bolt", "sparkles", "eye", "eye-off", "newspaper",
    "mail", "rss", "video", "play", "music", "mic", "camera", "image", "book",
    "graduation-cap", "briefcase", "wrench", "car", "home", "globe", "map-pin", "cart",
    "money", "chart", "cpu", "code", "flask", "shield", "leaf", "sun", "moon", "trophy",
    "gamepad", "filter",
)

MAX_PRESETS = 200
MAX_LABEL = 40
MAX_ID = 64
MAX_QUERY = 200
MAX_LIST = 500
MAX_AGE_RANGE = (1, 3650)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PRESET_FIELDS = {
    "id", "kind", "label", "icon", "display", "enabled", "position", "sort", "order",
    "show_count", "criteria",
}
_CRITERIA_FIELDS = {
    "status", "feed_ids", "category_ids", "source_kinds", "query", "max_age_days",
}


class PresetValidationError(ValueError):
    """Semantic validation failure; ``code`` is the stable machine-readable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- defaults -----------------------------------------------------------------------


def empty_criteria(status: str = "any") -> dict[str, Any]:
    return {
        "status": status,
        "feed_ids": [],
        "category_ids": [],
        "source_kinds": [],
        "query": None,
        "max_age_days": None,
    }


_DEFAULT_SPECS: tuple[tuple[str, str, str, str, str], ...] = (
    # id, icon, status, sort, order
    ("all", "list", "any", "published", "desc"),
    ("unread", "circle-dot", "unread", "published", "asc"),
    ("updated", "refresh", "updated", "sources", "desc"),
    ("saved", "bookmark", "saved", "saved_at", "desc"),
)


def default_preset(preset_id: str) -> dict[str, Any]:
    for position, (pid, icon, status, sort, order) in enumerate(_DEFAULT_SPECS):
        if pid == preset_id:
            return {
                "id": pid,
                "kind": "builtin",
                # null = clients show their localized built-in name
                "label": None,
                "icon": icon,
                "display": "icon+label",
                "enabled": True,
                "position": position,
                "sort": sort,
                "order": order,
                "show_count": True,
                "criteria": empty_criteria(status),
            }
    raise KeyError(preset_id)


def default_presets() -> list[dict[str, Any]]:
    return [default_preset(pid) for pid in BUILTIN_IDS]


def builtin_criteria(preset_id: str) -> dict[str, Any]:
    return empty_criteria(str(default_preset(preset_id)["criteria"]["status"]))


# --- keyword normalisation ----------------------------------------------------------


def normalize_text(text: str | None) -> str:
    """NFKD, strip combining marks, casefold (accent- and case-insensitive matching)."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def query_tokens(query: str | None) -> list[str]:
    """Whitespace-separated normalised tokens of a keyword query (all must match)."""
    return [t for t in normalize_text(query).split() if t]


def text_matches(title: str, summary: str, query: str | None) -> bool:
    tokens = query_tokens(query)
    if not tokens:
        return True
    haystack = normalize_text(f"{title} {summary}")
    return all(t in haystack for t in tokens)


# --- icon / grapheme ----------------------------------------------------------------


def _is_extend(ch: str) -> bool:
    cp = ord(ch)
    return (
        unicodedata.category(ch) in ("Mn", "Me")
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
    )


def _grapheme_count(text: str) -> int:
    """Approximate extended-grapheme count, enough to recognise single emoji sequences."""
    count = 0
    i = 0
    n = len(text)
    while i < n:
        count += 1
        ch = text[i]
        i += 1
        if 0x1F1E6 <= ord(ch) <= 0x1F1FF and i < n and 0x1F1E6 <= ord(text[i]) <= 0x1F1FF:
            i += 1  # flag = regional-indicator pair
        while i < n and _is_extend(text[i]):
            i += 1
        while i < n and text[i] == "\u200d":  # ZWJ joins the next symbol into the cluster
            i += 1
            if i < n:
                i += 1
            while i < n and _is_extend(text[i]):
                i += 1
    return count


def is_valid_icon(icon: str) -> bool:
    if icon in ICON_CATALOG:
        return True
    if not icon or len(icon) > 32 or any(c.isspace() for c in icon):
        return False
    first = icon[0]
    keycap = first in "0123456789#*" and icon.endswith("\u20e3")
    if first.isascii() and not keycap:
        return False
    if first.isalnum() and not keycap:
        return False
    return _grapheme_count(icon) == 1


# --- validation ---------------------------------------------------------------------


def _fail(code: str, message: str) -> PresetValidationError:
    return PresetValidationError(code, message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_criteria(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _fail("invalid_payload", "criteria must be an object")
    unknown = set(raw) - _CRITERIA_FIELDS
    if unknown:
        raise _fail("invalid_payload", f"unknown criteria field: {sorted(unknown)[0]}")
    out = empty_criteria()
    status = raw.get("status", "any")
    if status not in STATUSES:
        raise _fail("invalid_enum", "criteria.status must be any|unread|updated|saved")
    out["status"] = status
    for key, is_valid in (
        ("feed_ids", _is_int),
        ("category_ids", lambda v: isinstance(v, str) and 0 < len(v) <= 128),
        ("source_kinds", lambda v: v in SOURCE_KINDS),
    ):
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise _fail("invalid_payload", f"criteria.{key} must be a list")
        if len(value) > MAX_LIST:
            raise _fail("list_too_large", f"criteria.{key} holds at most {MAX_LIST} entries")
        for item in value:
            if not is_valid(item):
                code = "invalid_enum" if key == "source_kinds" else "invalid_payload"
                raise _fail(code, f"invalid entry in criteria.{key}")
        out[key] = list(dict.fromkeys(value))
    query = raw.get("query")
    if query is not None:
        if not isinstance(query, str):
            raise _fail("invalid_payload", "criteria.query must be a string or null")
        if len(query) > MAX_QUERY:
            raise _fail("query_too_long", f"criteria.query holds at most {MAX_QUERY} characters")
        query = query.strip() or None
    out["query"] = query
    max_age = raw.get("max_age_days")
    if max_age is not None:
        if not _is_int(max_age) or not MAX_AGE_RANGE[0] <= max_age <= MAX_AGE_RANGE[1]:
            raise _fail("invalid_max_age", "criteria.max_age_days must be an integer 1-3650")
    out["max_age_days"] = max_age
    return out


def _validate_preset(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _fail("invalid_payload", "each preset must be an object")
    unknown = set(raw) - _PRESET_FIELDS
    if unknown:
        raise _fail("invalid_payload", f"unknown preset field: {sorted(unknown)[0]}")
    missing = _PRESET_FIELDS - {"label", "position"} - set(raw)
    if missing:
        raise _fail("invalid_payload", f"missing preset field: {sorted(missing)[0]}")
    pid, kind = raw["id"], raw["kind"]
    if kind not in KINDS:
        raise _fail("invalid_enum", "kind must be builtin|custom")
    if not isinstance(pid, str) or not pid or len(pid) > MAX_ID or not _ID_RE.match(pid):
        raise _fail("invalid_id", "id must be 1-64 characters of [A-Za-z0-9_-]")
    if (kind == "builtin") != (pid in BUILTIN_IDS):
        raise _fail("invalid_id", "builtin kind requires a builtin id (and only builtins use them)")
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise _fail("invalid_label", "label must be a string or null")
    label = label.strip() if isinstance(label, str) else None
    if label and len(label) > MAX_LABEL:
        raise _fail("invalid_label", f"label holds at most {MAX_LABEL} characters")
    if kind == "custom" and not label:
        raise _fail("invalid_label", "custom presets need a label of 1-40 characters")
    icon = raw["icon"]
    if not isinstance(icon, str) or not is_valid_icon(icon):
        raise _fail("invalid_icon", "icon must be a catalog key or a single emoji")
    for key, allowed in (("display", DISPLAYS), ("sort", SORTS), ("order", ORDERS)):
        if raw[key] not in allowed:
            raise _fail("invalid_enum", f"{key} must be one of {', '.join(allowed)}")
    for key in ("enabled", "show_count"):
        if not isinstance(raw[key], bool):
            raise _fail("invalid_payload", f"{key} must be a boolean")
    position = raw.get("position", index)
    if not _is_int(position):
        raise _fail("invalid_payload", "position must be an integer")
    criteria = _validate_criteria(raw["criteria"])
    if kind == "builtin" and criteria != builtin_criteria(pid):
        raise _fail("builtin_criteria_locked", "built-in presets have fixed criteria")
    return {
        "id": pid,
        "kind": kind,
        "label": label or None,
        "icon": icon,
        "display": raw["display"],
        "enabled": raw["enabled"],
        "position": position,
        "sort": raw["sort"],
        "order": raw["order"],
        "show_count": raw["show_count"],
        "criteria": criteria,
    }


def validate_presets(raw: Any) -> list[dict[str, Any]]:
    """Validate a whole preset list; return it normalised (dense positions, deduped lists)."""
    if not isinstance(raw, list):
        raise _fail("invalid_payload", "presets must be a list")
    if len(raw) > MAX_PRESETS:
        raise _fail("too_many_presets", f"at most {MAX_PRESETS} presets")
    presets = [_validate_preset(item, i) for i, item in enumerate(raw)]
    ids = [p["id"] for p in presets]
    if len(set(ids)) != len(ids):
        raise _fail("duplicate_id", "preset ids must be unique")
    missing = [b for b in BUILTIN_IDS if b not in ids]
    if missing:
        raise _fail("builtin_missing", f"built-in preset missing: {missing[0]}")
    if not any(p["enabled"] for p in presets):
        raise _fail("no_enabled_preset", "at least one preset must stay enabled")
    # stable sort: equal positions keep their array order
    presets.sort(key=lambda p: p["position"])
    for position, preset in enumerate(presets):
        preset["position"] = position
    return presets


def validate_compact(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _fail("invalid_payload", "compact must be a boolean")
    return value


def normalize_last_preset_id(value: Any, presets: Iterable[Mapping[str, Any]]) -> str | None:
    """null / unknown ids (e.g. a deleted preset) collapse to null; clients then fall back."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail("invalid_payload", "last_preset_id must be a string or null")
    return value if any(p["id"] == value for p in presets) else None


def new_custom_id() -> str:
    return str(uuid.uuid4())


# --- storage ------------------------------------------------------------------------


def load_presets(stored: str | None) -> list[dict[str, Any]]:
    """Stored JSON → presets; NULL / unreadable → defaults."""
    if not stored:
        return default_presets()
    try:
        return validate_presets(json.loads(stored))
    except (ValueError, TypeError):
        return default_presets()


def dump_presets(presets: list[dict[str, Any]]) -> str:
    return json.dumps(presets, ensure_ascii=False, separators=(",", ":"))


def reset_builtin(presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore the four built-ins to their defaults (first, in default order); keep customs."""
    customs = [p for p in presets if p["kind"] == "custom"]
    return validate_presets(default_presets() + customs)


def visible_presets(presets: list[dict[str, Any]], feed_ids: set[int]) -> list[dict[str, Any]]:
    """Copy with feed ids the user cannot see (deleted / unsubscribed) dropped."""
    out: list[dict[str, Any]] = []
    for preset in presets:
        copy = {**preset, "criteria": dict(preset["criteria"])}
        copy["criteria"]["feed_ids"] = [f for f in preset["criteria"]["feed_ids"] if f in feed_ids]
        out.append(copy)
    return out


# --- criteria evaluation (reference semantics) --------------------------------------

def effective_source_kind(kind: str, url: str) -> str:
    """Source kind of a feed: YouTube channel feeds are RSS feeds with a YouTube URL."""
    from app.services import youtube

    if kind == "rss" and youtube.is_channel_feed_url(url):
        return "youtube"
    return kind


@dataclass(frozen=True)
class EvalStory:
    """A story as seen by one user, for pure evaluation (vectors, Apple parity)."""

    id: int
    title: str
    summary: str
    category: str
    version: int
    last_updated_at: datetime
    article_feed_ids: tuple[int, ...]
    article_published: tuple[datetime | None, ...]
    # Link of each source article, parallel to ``article_feed_ids`` (YouTube video detection).
    article_links: tuple[str | None, ...] = ()
    is_read: bool = False
    read_at_version: int = 0
    saved_at: datetime | None = None


def is_youtube_video_link(link: str | None) -> bool:
    from app.services import youtube

    return bool(link) and youtube.parse_video_id(link or "") is not None


def _matches_source_kinds(
    story: EvalStory, kinds: list[str], feed_kinds: Mapping[int, str]
) -> bool:
    """A feed of a listed kind, or (for ``youtube``) a YouTube video link on an article of a
    subscribed feed. Kinds are not disjoint: a shared YouTube link is both ``saved`` and
    ``youtube``."""
    if any(feed_kinds.get(f) in kinds for f in set(story.article_feed_ids)):
        return True
    if "youtube" not in kinds:
        return False
    return any(
        feed in feed_kinds and is_youtube_video_link(link)
        for feed, link in zip(story.article_feed_ids, story.article_links, strict=False)
    )


def story_matches(
    story: EvalStory,
    criteria: Mapping[str, Any],
    feed_kinds: Mapping[int, str],
    now: datetime,
) -> bool:
    status = criteria.get("status", "any")
    if status == "unread" and story.is_read:
        return False
    if status == "updated" and not (story.is_read and story.version > story.read_at_version):
        return False
    if status == "saved" and story.saved_at is None:
        return False
    feeds = set(story.article_feed_ids)
    feed_ids = criteria.get("feed_ids") or []
    if feed_ids and not feeds & set(feed_ids):
        return False
    categories = criteria.get("category_ids") or []
    if categories and story.category not in categories:
        return False
    kinds = criteria.get("source_kinds") or []
    if kinds and not _matches_source_kinds(story, kinds, feed_kinds):
        return False
    max_age = criteria.get("max_age_days")
    if max_age is not None and story.last_updated_at < now - timedelta(days=max_age):
        return False
    return text_matches(story.title, story.summary, criteria.get("query"))


def sort_stories(
    stories: Iterable[EvalStory], sort: str, order: str
) -> list[EvalStory]:
    """Order like the SQL list: key, then last_updated_at, then id, all in ``order``."""
    reverse = order == "desc"

    def key(story: EvalStory) -> tuple[Any, ...]:
        if sort == "published":
            dates = [d for d in story.article_published if d is not None]
            primary: Any = min(dates) if dates else story.last_updated_at
        elif sort == "sources":
            primary = len(story.article_feed_ids)
        elif sort == "saved_at":
            # missing saved_at counts as the smallest value: first when asc, last when desc
            primary = (story.saved_at is not None, story.saved_at or story.last_updated_at)
        else:
            return (story.last_updated_at, story.id)
        return (primary, story.last_updated_at, story.id)

    return sorted(stories, key=key, reverse=reverse)
