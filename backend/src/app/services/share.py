"""Story sharing (always available — no external service needed).

Builds a plain-text share card (headline + merged summary + all source links,
like the Readeck card) and optionally translates headline + summary into a
target language via the LLM. The frontend hands the result to the Web Share
API (navigator.share) with a clipboard fallback, so nothing leaves the server
except the LLM translation call.

The target languages come from `share_languages` (comma-separated ISO codes,
filtered to the known LANGUAGE_NAMES) — configurable per invariant 5. Sharing
"as is" never calls the LLM.
"""

from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.services import llm_client, prompts

if TYPE_CHECKING:
    from app.models import Article, Story


class ShareError(RuntimeError):
    pass


def available_languages() -> list[dict[str, str]]:
    """Selectable share languages: [{code, name}] from SHARE_LANGUAGES."""
    out: list[dict[str, str]] = []
    for code in (c.strip() for c in settings.share_languages.split(",")):
        name = prompts.LANGUAGE_NAMES.get(code)
        if name and code not in {lang["code"] for lang in out}:
            out.append({"code": code, "name": name})
    return out


def render_share_text(title: str, summary: str, articles: list["Article"]) -> str:
    """Plain-text card: title, summary, then one source per line (URL alone on
    the line so chat apps render a link preview)."""
    lines = [title, "", summary, "", "Sources:"]
    for a in articles:
        lines.append(f"- {a.title or a.url}")
        lines.append(f"  {a.url}")
    return "\n".join(lines)


async def _translate(
    title: str, summary: str, target_language: str
) -> tuple[dict[str, Any], int]:
    """LLM seam (module-level for monkeypatching in tests)."""
    system, user = prompts.translate_story_text(title, summary, target_language)
    return await llm_client.chat_json(system, user)


async def prepare_share(
    story: "Story", articles: list["Article"], language: str | None
) -> dict[str, Any]:
    """Build the share payload. `language` = ISO code from available_languages()
    or None/empty for the original SUMMARY_LANGUAGE text (no LLM call)."""
    if not articles:
        raise ShareError("Story has no articles")
    title, summary = story.title, story.summary
    translated = False
    latency_ms = 0
    if language:
        allowed = {lang["code"]: lang["name"] for lang in available_languages()}
        name = allowed.get(language)
        if name is None:
            raise ShareError(f"Unsupported share language: {language}")
        if language != settings.summary_language:
            data, latency_ms = await _translate(title, summary, name)
            t, s = data.get("title"), data.get("summary")
            if not (isinstance(t, str) and t.strip() and isinstance(s, str) and s.strip()):
                raise ShareError("LLM returned an incomplete translation")
            title, summary, translated = t.strip(), s.strip(), True
    # primary source = earliest published, else first (same rule as Readeck)
    primary = min(
        articles, key=lambda a: (a.published_at is None, a.published_at or a.id)
    )
    return {
        "title": title,
        "text": render_share_text(title, summary, articles),
        "url": primary.url,
        "language": language or settings.summary_language,
        "translated": translated,
        "latency_ms": latency_ms,
    }
