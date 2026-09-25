"""All LLM prompts live here (SPEC §8). Every prompt requests structured JSON.

Language invariant: summaries/headlines are written in `SUMMARY_LANGUAGE` — the
language name is injected into prompts, never hardcoded.
"""

from typing import Any

from app.core.config import settings

LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ar": "Arabic",
    "ko": "Korean",
    "pl": "Polish",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
}


def summary_language_name(lang_code: str | None = None) -> str:
    code = lang_code or settings.summary_language
    return LANGUAGE_NAMES.get(code, code)


def summarize_article(
    title: str, text: str, taxonomy: list[str], lang_code: str | None = None
) -> tuple[str, str]:
    """Per-article summary + category. Returns (system, user)."""
    lang = summary_language_name(lang_code)
    categories = ", ".join(taxonomy)
    system = (
        f"You are a news summarizer for a personal news reader. Always write in {lang}. "
        "Reply with ONLY a valid JSON object."
    )
    user = f"""Summarize the following news article in {lang} (2-4 sentences, factual, no opinion).

Also assign exactly one category from this list: {categories}

If — and only if — none of the listed categories fit this article well, also
propose a short new category name (2-3 words, in {lang}) in "suggested_category".
Otherwise set "suggested_category" to null. Do not propose a new category just
because a listed one is a loose fit; only when the article is clearly about a
recurring topic none of them cover.

Reply with JSON: {{"summary": "...", "category": "...", "suggested_category": "... or null"}}

Article title: {title}

Article text:
{text[:8000]}"""
    return system, user


def story_headline(article_summaries: list[str], lang_code: str | None = None) -> tuple[str, str]:
    """Generate a short story headline from member article summaries."""
    lang = summary_language_name(lang_code)
    joined = "\n\n".join(f"- {s}" for s in article_summaries[:10])
    system = (
        f"You write short, factual news headlines in {lang}. Reply with ONLY a valid JSON object."
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


def merge_story_summary(
    old_summary: str, new_article_summary: str, lang_code: str | None = None
) -> tuple[str, str]:
    """Merge a new article's facts into a story; also refresh the headline."""
    lang = summary_language_name(lang_code)
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


def translate_story_text(title: str, summary: str, target_language: str) -> tuple[str, str]:
    """Translate a story headline + summary into the target language (sharing)."""
    system = (
        "You are a professional translator. Translate faithfully, keep the tone "
        "factual and neutral, and never add or drop facts. "
        "Reply with ONLY a valid JSON object."
    )
    user = f"""Translate the following news headline and summary into {target_language}.
Keep proper nouns (people, places, organizations) in their standard form.

Reply with JSON: {{"title": "...", "summary": "..."}}

Headline: {title}

Summary:
{summary}"""
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


def newsletter_clean(subject: str, body: str) -> tuple[str, str]:
    """Pass 1 of newsletter processing: delete everything that is not news.

    The body is the FULL email rendered as text where every link is a
    placeholder token ([visible text](«L42») — never the real URL, which is
    hundreds of characters of tracking junk the model doesn't need to judge
    the link's role). The model only DELETES chrome (intro, socials, sponsors,
    platform self-links); surviving placeholders are resolved back to URLs by
    code (mailnews._llm_clean_filter). Deletion-only is far more reliable for
    small models than per-link triage in one structured pass.
    """
    system = (
        "You clean up email newsletters for a personal news reader. "
        "You never add, translate or rephrase text: you only delete. "
        "Reply with ONLY the cleaned newsletter content, in the same language."
    )
    user = f"""Below is the full content of a newsletter email (subject: {subject}),
rendered as text. Each link appears as [visible text](«L42»): the «L42» token
is just a numbered reference to a web URL — never delete or modify the tokens
themselves, and never invent new ones.

Delete everything that is NOT curated news content:
- the greeting/introduction, personal notes, announcements about the newsletter
  itself (schedule, account renames, accessibility notes, thank-you lists) and
  the sign-off;
- sponsor / patron / "mécènes" credits;
- the social-media and footer block (the author's own YouTube, Twitch, Podcast,
  Instagram, TikTok, Threads, Bluesky, Discord, website links);
- any link back to the newsletter's own platform or site (Patreon, the
  author's own site) and app-download links (Google Play / App Store).

Keep ONLY the news section: each news item with its link token and the
author's description, COPIED VERBATIM (same language, same words, one item per
paragraph). Do not summarize, translate or reorder.

Newsletter content:
{body}"""
    return system, user


def newsletter_extract(
    sender: str, subject: str, items: list[tuple[str, str, str]]
) -> tuple[str, str]:
    """Map pre-filtered newsletter links to (title, verbatim intro).

    Filtering is pass 1's job (newsletter_clean): the links handed here are
    already news. items = (url, anchor_text, nearby_text) pre-extracted from
    the HTML by code — the LLM may only pick from these URLs (hallucinated
    URLs are dropped by the caller). The intro is COPIED VERBATIM from the
    newsletter (never paraphrased or invented), kept in the newsletter's own
    language ON PURPOSE: it is displayed as-is when it becomes a new story's
    summary (the LLM summary still drives embeddings/clustering per
    invariant 2).
    """
    lines = "\n".join(
        f"- URL: {url}\n  anchor: {anchor}\n  context: {context}" for url, anchor, context in items
    )
    system = (
        "You curate article links from email newsletters for a personal news reader. "
        "Reply with ONLY a valid JSON object."
    )
    user = f"""This newsletter from {sender} (subject: {subject}) points its readers at
the links below — they are already filtered down to real news items.

For each link, fill:
- "title": a short factual title for the linked article. Do NOT just copy the
  anchor when it is only a site/domain name — compose a real headline from the
  context instead;
- "intro": the EXACT sentence(s) the newsletter writes about this link, COPIED
  VERBATIM from the context below. Do NOT summarize, translate, rephrase or
  invent text — copy the newsletter's own words (you may trim the anchor text
  itself and list markers). Empty string only if the newsletter says nothing
  about the link.

Only use URLs from the list below, exactly as given.

Reply with JSON: {{"items": [{{"url": "...", "title": "...", "intro": "..."}}]}}

Links:
{lines}"""
    return system, user


def chat_answer(
    question: str,
    stories: list[tuple[int, str, str, str, str]],
) -> tuple[str, str]:
    """RAG answer over retrieved stories. Each story is
    (id, title, summary, category, last_updated_iso)."""
    lang = summary_language_name()
    system = (
        "You are the assistant of a personal news reader. Answer questions using "
        "ONLY the provided news stories — never outside knowledge. If the stories "
        "don't cover the question, say so honestly. "
        f"Always write in {lang}. Reply with ONLY a valid JSON object."
    )
    blocks = []
    for sid, title, summary, category, updated in stories:
        blocks.append(f"[Story {sid}] ({category}, updated {updated})\n{title}\n{summary}")
    context = "\n\n".join(blocks)
    user = f"""Answer the user's question in {lang}, using only these stories retrieved
from the user's news archive (2-6 sentences, factual, no opinion). Cite every story
you actually used by its id.

Reply with JSON: {{"answer": "...", "story_ids": [<ids of cited stories>]}}

Stories:
{context}

Question: {question}"""
    return system, user


def discovery_queries(
    location: str,
    themes: list[str],
    query: str,
    lang_code: str | None = None,
) -> tuple[str, str]:
    """Generate search queries, candidate publication domains, and probable feed URLs.
    Returns (system, user)."""
    lang = summary_language_name(lang_code)
    system = (
        f"You are an expert news aggregator research assistant. Respond in {lang}. "
        "Reply with ONLY a valid JSON object."
    )
    themes_str = ", ".join(themes) if themes else "General News"
    loc_str = location.strip() if location.strip() else "Global / International"
    q_str = query.strip() if query.strip() else "None provided"

    user = f"""The user wants to discover high-quality RSS/Atom feeds matching their criteria:
- Location / Region: {loc_str}
- Themes / Categories: {themes_str}
- Custom request: {q_str}

Analyze the geographic granularity of the request:
- "scope_level": classify into "city", "region", "country", "continent", or "global".
  Examples: "Lyon, France" -> city, "Texas" -> region, "France" -> country, "Europe" -> continent, empty/"global" -> global.
- "target_entity": the primary city or place name (e.g. "Lyon", "France", "Europe").

Important location instructions:
If scope is "city" or "region" (e.g. "{loc_str}"), focus specifically on local newspapers,
regional daily publications, city portals, and local broadcasters from that specific area.
Do NOT propose generic national or global outlets (such as BBC, Reuters, NPR, Le Monde, CNN) unless the scope is country/global.

Suggest targeted web search queries to locate RSS/Atom feeds, top publication domains
covering these topics in this region, and any known candidate feed URLs.

Reply with JSON:
{{
  "scope_level": "city|region|country|continent|global",
  "target_entity": "...",
  "search_queries": ["query 1", "query 2", "query 3"],
  "suggested_domains": ["example.com", "news-site.org"],
  "candidate_feed_urls": ["https://example.com/rss", "https://news-site.org/feed"]
}}"""
    return system, user


def discovery_synthesis(
    candidates: list[dict[str, Any]],
    location: str,
    scope_level: str,
    themes: list[str],
    query: str,
    lang_code: str | None = None,
) -> tuple[str, str]:
    """Rank, annotate, and describe discovered validated feeds with access level and geographic scope.
    Returns (system, user)."""
    lang = summary_language_name(lang_code)
    system = (
        f"You curate RSS/Atom feeds for a personal news reader. Respond in {lang}. "
        "Reply with ONLY a valid JSON object."
    )
    themes_str = ", ".join(themes) if themes else "General News"
    loc_str = location.strip() if location.strip() else "Global / International"
    q_str = query.strip() if query.strip() else "None provided"

    items_text = []
    for c in candidates:
        samples = ", ".join(f'"{t}"' for t in c.get("sample_titles", [])[:3])
        access_hint = c.get("access_level", "unknown")
        items_text.append(
            f"- URL: {c.get('url')}\n"
            f"  Title: {c.get('title')}\n"
            f"  Site: {c.get('site_url') or 'unknown'}\n"
            f"  Description: {c.get('description') or 'none'}\n"
            f"  Detected Access: {access_hint}\n"
            f"  Recent article headlines: {samples or 'none'}"
        )
    joined_items = "\n\n".join(items_text)

    user = f"""The user is searching for feeds with:
- Location: {loc_str} (Scope level: {scope_level})
- Themes: {themes_str}
- Custom query: {q_str}

Below are verified live RSS/Atom feeds that were discovered:
{joined_items}

Important instructions:
1. GEOGRAPHIC RELEVANCE:
- If scope level is "city" or "region" (e.g. "{loc_str}"), candidate feeds MUST specifically focus on that city or regional area.
  Assign "geographic_scope": "local" | "regional" | "national" | "global".
  Feeds that are broad national or global outlets without local focus should be designated "national" or "global".
- If scope level is "country", national publications are expected.
- If scope level is "global" or "continent", international publications are expected.

2. ACCESS LEVEL (PAYWALL VS FREE):
Determine whether each publication requires a subscription or is freely accessible:
- "paywalled": publisher requires a paid subscription / paywall (e.g. Le Monde, Financial Times, Mediapart, NYT) or marked reserved for subscribers.
- "free_excerpt": free to access, but RSS articles only provide short excerpts/summaries rather than full content.
- "free_full": 100% free with full article text in the feed.

For each feed, output:
- "url": exact feed URL as provided
- "title": cleaned up, recognizable publication title
- "description": a concise 1-2 sentence description in {lang} of what this publication covers
- "match_reason": a short explanation (1 sentence in {lang}) of why it matches the user's location, themes, or custom query
- "access_level": "free_full" | "free_excerpt" | "paywalled"
- "geographic_scope": "local" | "regional" | "national" | "global"

Reply with JSON:
{{
  "feeds": [
    {{
      "url": "...",
      "title": "...",
      "description": "...",
      "match_reason": "...",
      "access_level": "free_full|free_excerpt|paywalled",
      "geographic_scope": "local|regional|national|global"
    }}
  ]
}}"""
    return system, user
