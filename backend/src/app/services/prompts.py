"""All LLM prompts live here (SPEC §8). Every prompt requests structured JSON.

Language invariant: summaries/headlines are written in `SUMMARY_LANGUAGE` — the
language name is injected into prompts, never hardcoded.
"""

from app.core.config import settings

LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
}


def summary_language_name() -> str:
    return LANGUAGE_NAMES.get(settings.summary_language, settings.summary_language)


def summarize_article(title: str, text: str, taxonomy: list[str]) -> tuple[str, str]:
    """Per-article summary + category. Returns (system, user)."""
    lang = summary_language_name()
    categories = ", ".join(taxonomy)
    system = (
        f"You are a news summarizer for a personal news reader. Always write in {lang}. "
        "Reply with ONLY a valid JSON object."
    )
    user = f"""Summarize the following news article in {lang} (2-4 sentences, factual, no opinion).

Also assign exactly one category from this list: {categories}

Reply with JSON: {{"summary": "...", "category": "..."}}

Article title: {title}

Article text:
{text[:8000]}"""
    return system, user


def story_headline(article_summaries: list[str]) -> tuple[str, str]:
    """Generate a short story headline from member article summaries."""
    lang = summary_language_name()
    joined = "\n\n".join(f"- {s}" for s in article_summaries[:10])
    system = (
        f"You write short, factual news headlines in {lang}. "
        "Reply with ONLY a valid JSON object."
    )
    user = f"""These summaries describe the same news story:

{joined}

Write one short headline (max 12 words) in {lang} capturing the story.

Reply with JSON: {{"headline": "..."}}"""
    return system, user


def novelty_check(story_summary: str, new_article_summary: str) -> tuple[str, str]:
    """Does the new article add facts to the story? (SPEC §5)"""
    system = "You compare news summaries. Reply with ONLY a valid JSON object."
    user = f"""Current story summary:
{story_summary}

New article summary:
{new_article_summary}

Does the new article add any NEW facts or developments not already covered by the
story summary?

Reply with JSON: {{"new_facts": true|false, "added": "short description or empty"}}"""
    return system, user


def merge_story_summary(old_summary: str, new_article_summary: str) -> tuple[str, str]:
    """Merge a new article's facts into a story; also refresh the headline."""
    lang = summary_language_name()
    system = (
        f"You merge news summaries into a single coherent summary in {lang}. "
        "Reply with ONLY a valid JSON object."
    )
    user = f"""Merge the new information into the existing story summary. Keep it concise
(3-5 sentences), factual, in {lang}. Drop redundant wording.

Also write one short headline (max 12 words) in {lang} capturing the updated story.

Existing story summary:
{old_summary}

New article summary:
{new_article_summary}

Reply with JSON: {{"summary": "...", "headline": "..."}}"""
    return system, user


def pairwise_same_event(summary_a: str, summary_b: str) -> tuple[str, str]:
    """Gray-zone clustering confirmation (SPEC §4)."""
    system = (
        "You decide whether two news items report the same event. "
        "Reply with ONLY a valid JSON object."
    )
    user = f"""Item A:
{summary_a}

Item B:
{summary_b}

Do both items report on the SAME specific news event (not just the same topic)?

Reply with JSON: {{"same_event": true|false}}"""
    return system, user
