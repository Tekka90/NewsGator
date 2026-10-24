"""Listen: text-only cleaning and translation of an article's full text.

Cleaning is pure, deterministic and language-agnostic: it only uses structural heuristics
(markup, URLs, link density, line shape), never keyword lists or site names. The stored
`article.full_text` is never modified and nothing cleaned is persisted (computing is cheap).

Translation is chunked by paragraphs, goes through the LLM client wrapper like
services/translation.py and is cached per (article_id, target language) in
`article_translation`, keyed by a hash of the cleaned source text.
"""

import hashlib
import html
import re
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Article, ArticleTranslation
from app.services import activity, llm_budget, llm_client, llmtrace, prompts, usage

MIN_CLEAN_RATIO = 0.2  # cleaned/raw length below this → the text was mostly residue
PROSE_MIN_CHARS = 80  # a line at least this long counts as prose
MENU_MAX_CHARS = 40  # a short line without sentence punctuation looks like a menu entry
MENU_MAX_WORDS = 5
MENU_RUN_DROP = 3  # runs of this many menu-like lines are dropped even next to prose
LINK_DENSITY_MAX = 0.5

_BLOCK_RESIDUE_RE = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|template|svg|iframe)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_BREAK_RE = re.compile(
    r"</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|blockquote)\b[^>]*>", re.IGNORECASE
)
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>|<!--.*?-->", re.DOTALL)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_URL_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)[^\s<>\")\]]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_FOOTNOTE_RE = re.compile(r"\[\^?\s*\d{1,3}\s*\]|\[\s*[a-z]\s*\]")
_MD_MARK_RE = re.compile(r"(\*{1,3}|_{2,3}|`+|~~)")
_LINE_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|>+\s*|[-*+•·▪●◦‣]\s+|\d{1,3}[.)]\s+(?=\S))")
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0e\ufe0f\u200d\u20e3\U000e0020-\U000e007f]+"
)
_INVISIBLE_RE = re.compile("[\u200b\u200c\u2060\ufeff\u00ad]")
_SPACE_RE = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u3000]+")
_SENTENCE_END = ".!?…:;\u3002\uff01\uff1f\u061f\u0964"
_LETTER_RE = re.compile(r"\w", re.UNICODE)


def _link_chars(line: str) -> int:
    total = sum(len(m.group(0)) for m in _MD_LINK_RE.finditer(line))
    rest = _MD_LINK_RE.sub("", line)
    return total + sum(len(m.group(0)) for m in _URL_RE.finditer(rest))


def _strip_markup(text: str) -> str:
    text = _BLOCK_RESIDUE_RE.sub("\n", text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(html.unescape(text))  # double-encoded entities are common in feeds


def _clean_line(line: str) -> str:
    line = _MD_IMAGE_RE.sub("", line)
    line = _MD_LINK_RE.sub(lambda m: m.group(1), line)
    line = _URL_RE.sub("", line)
    line = _EMAIL_RE.sub("", line)
    line = _FOOTNOTE_RE.sub("", line)
    line = _LINE_PREFIX_RE.sub("", line)
    line = _MD_MARK_RE.sub("", line)
    line = _EMOJI_RE.sub("", line)
    line = _INVISIBLE_RE.sub("", line)
    line = _SPACE_RE.sub(" ", line).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", line)


def _is_prose(line: str) -> bool:
    return len(line) >= PROSE_MIN_CHARS


def _is_menu_like(line: str) -> bool:
    return (
        len(line) <= MENU_MAX_CHARS
        and len(line.split()) <= MENU_MAX_WORDS
        and line[-1] not in _SENTENCE_END
    )


def _drop_menu_lines(lines: list[str]) -> list[str]:
    """Drop menu-like short lines: long runs always, short runs unless next to prose."""
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if not _is_menu_like(lines[i]):
            kept.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and _is_menu_like(lines[j]):
            j += 1
        run = lines[i:j]
        before = lines[i - 1] if i > 0 else ""
        after = lines[j] if j < len(lines) else ""
        if len(run) < MENU_RUN_DROP and (_is_prose(before) or _is_prose(after)):
            kept.extend(run)
        i = j
    return kept


def clean(text: str | None) -> str:
    """Speakable plain text from stored article text; paragraphs separated by blank lines."""
    if not text:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in _strip_markup(text).splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        link_chars = _link_chars(stripped)
        if link_chars and link_chars / len(stripped) > LINK_DENSITY_MAX:
            continue
        line = _clean_line(stripped)
        if not line or not _LETTER_RE.search(line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n\n".join(_drop_menu_lines(lines))


def usable_text(content_status: str, full_text: str | None) -> str:
    """Cleaned text when the article qualifies for Listen, else ''. Never touches paywalls:
    only `full` articles qualify, and the cleaned text must pass the quality gate."""
    if content_status != "full" or not full_text:
        return ""
    cleaned = clean(full_text)
    if len(cleaned) < settings.fulltext_min_chars:
        return ""
    if len(cleaned) / len(full_text) < MIN_CLEAN_RATIO:
        return ""
    return cleaned


def full_text_info(article: Article) -> tuple[bool, int]:
    """(has_full_text, char_count) for the story-detail payload."""
    cleaned = clean(article.full_text)
    ok = bool(usable_text(article.content_status, article.full_text))
    return ok, len(cleaned)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _translate_chunk(chunk: str, target_name: str) -> tuple[str, int]:
    """Low-level LLM call seam (module-level for test monkeypatching)."""
    system, user = prompts.translate_article_text(chunk, target_name)
    with llmtrace.context("article_translate", label=f"listen → {target_name}"):
        return await llm_client.chat_text(system, user)


class TranslationTooLongError(Exception):
    pass


async def translate_article_text(
    session: AsyncSession, article: Article, cleaned: str, target_language: str
) -> str:
    """Translate `cleaned` into `target_language`, using/refreshing the per-article cache.

    Raises llm_client.LLMError on provider failure and TranslationTooLongError when the text
    would need more LLM calls than llm_budget.MAX_CALLS_PER_ARTICLE.
    """
    source_hash = _hash(cleaned)
    cached = await session.get(ArticleTranslation, (article.id, target_language))
    if cached is not None and cached.source_hash == source_hash:
        return cached.text

    target_name = prompts.LANGUAGE_NAMES.get(target_language, target_language)
    system, _ = prompts.translate_article_text("", target_name)
    # Output mirrors input, so both must fit the context together.
    chunks = llm_budget.split_paragraphs(cleaned, llm_budget.input_budget_chars(system) // 2)
    if len(chunks) > llm_budget.MAX_CALLS_PER_ARTICLE:
        raise TranslationTooLongError(f"article needs {len(chunks)} translation calls")

    await activity.emit(
        session,
        "translation",
        "article_translate_start",
        {
            "article_id": article.id,
            "language": target_language,
            "chars": len(cleaned),
            "chunks": len(chunks),
        },
    )
    await session.commit()
    parts: list[str] = []
    try:
        for chunk in chunks:
            part, latency_ms = await _translate_chunk(chunk, target_name)
            part = part.strip()
            if not part:
                raise llm_client.LLMError("empty translation")
            parts.append(part)
            usage.record(
                session,
                "article_translate",
                endpoint="chat",
                model=settings.llm_model,
                latency_ms=latency_ms,
                article=article,
                prompt_chars=len(system) + len(chunk),
                completion_chars=len(part),
            )
    except llm_client.LLMError as exc:
        await activity.emit(
            session,
            "translation",
            "article_translate_error",
            {"article_id": article.id, "language": target_language, "error": str(exc)[:300]},
            level="error",
        )
        await session.commit()
        raise

    translated = "\n\n".join(parts)
    now = datetime.now(UTC)
    if cached is None:
        session.add(
            ArticleTranslation(
                article_id=article.id,
                language=target_language,
                source_hash=source_hash,
                text=translated,
                created_at=now,
            )
        )
    else:
        cached.source_hash = source_hash
        cached.text = translated
        cached.created_at = now
    await activity.emit(
        session,
        "translation",
        "article_translated",
        {"article_id": article.id, "language": target_language, "chars": len(translated)},
    )
    await session.commit()
    return translated
