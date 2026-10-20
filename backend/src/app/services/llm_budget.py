"""Context-window budgeting for article text embedded in prompts (YOUTUBE_PLAN Phase 1A).

`LLM_CONTEXT_TOKENS` is the model's usable window. The room left for variable text is
`(window - reserved output - tokens(system + fixed prompt)) * CHARS_PER_TOKEN`. The ratio is
deliberately conservative (French/German/CJK-safe) and works with servers that omit `usage`.
"""

import math
import re

from app.core.config import settings

CHARS_PER_TOKEN = 3.0
RESERVED_OUTPUT_TOKENS = 2000
MIN_CONTEXT_TOKENS = 2048
MIN_BUDGET_CHARS = 1000
MAX_CALLS_PER_ARTICLE = 16

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…\u3002\uff01\uff1f])\s+")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def context_tokens() -> int:
    return max(int(settings.llm_context_tokens), MIN_CONTEXT_TOKENS)


def input_budget_chars(system: str, fixed_user_text: str = "") -> int:
    """Characters of variable text that fit next to `system` + `fixed_user_text`."""
    tokens = context_tokens() - RESERVED_OUTPUT_TOKENS - estimate_tokens(system + fixed_user_text)
    return max(int(tokens * CHARS_PER_TOKEN), MIN_BUDGET_CHARS)


def fits(text: str, budget_chars: int) -> bool:
    return len(text) <= budget_chars


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split one overlong paragraph on sentence boundaries, then words, then characters."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_split_words(sentence, max_chars))
            continue
        candidate = f"{current} {sentence}" if current else sentence
        if len(candidate) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _split_words(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > max_chars:  # no whitespace at all (e.g. CJK): cut by characters
            if current:
                pieces.append(current)
                current = ""
            pieces.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}" if current else word
        if len(candidate) > max_chars:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def split_paragraphs(text: str, max_chars: int) -> list[str]:
    """Chunks of at most `max_chars`, split on blank lines; overlong paragraphs are cut on
    sentence boundaries (never mid-word unless a single word exceeds the limit)."""
    max_chars = max(max_chars, 1)
    units: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        units.extend(
            _split_long(paragraph, max_chars) if len(paragraph) > max_chars else [paragraph]
        )
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def sample_evenly[T](items: list[T], count: int) -> list[T]:
    """`count` items spread evenly over the list (first and last included), order kept."""
    if count >= len(items):
        return list(items)
    if count <= 1:
        return items[len(items) // 2 : len(items) // 2 + 1] if items else []
    step = (len(items) - 1) / (count - 1)
    return [items[round(i * step)] for i in range(count)]


def sample_text(text: str, max_chars: int) -> str:
    """Last resort when digests still exceed the budget: keep paragraphs spread evenly over the
    whole text (never only the beginning)."""
    if len(text) <= max_chars:
        return text
    paragraphs = [p for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = split_paragraphs(text, max(max_chars // 8, 1))
    avg = max(len(text) // len(paragraphs), 1)
    count = min(len(paragraphs), max_chars // avg + 1)
    while count > 1:
        kept = sample_evenly(paragraphs, count)
        joined = "\n\n".join(kept)
        if len(joined) <= max_chars:
            return joined
        count -= 1
    return paragraphs[0][:max_chars]
