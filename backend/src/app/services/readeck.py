"""Readeck integration (optional).

Pushes a story to a self-hosted Readeck instance as a permanent bookmark:
the entry's content is the generated HTML (headline + merged summary + all
source links) uploaded via multipart `POST /api/bookmarks`, so it survives
NewsGator's retention window. The bookmark's canonical `url` is the primary
source article — Readeck never fetches it, the uploaded HTML is the content.

Enabled only when BOTH readeck_base_url and readeck_token are set (env or
settings overrides). All HTTP goes through the module-level `_post_bookmark`
seam so tests can monkeypatch it.
"""

import html as html_module
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.core.config import settings

if TYPE_CHECKING:
    from app.models import Article, Story


class ReadeckError(RuntimeError):
    pass


def is_enabled() -> bool:
    """Feature is on only when URL + token are both configured."""
    return bool(settings.readeck_base_url and settings.readeck_token)


def _summary_to_html(summary: str) -> str:
    """Plain-text summary → minimal HTML paragraphs."""
    paragraphs = [p.strip() for p in summary.split("\n\n") if p.strip()]
    return "".join(f"<p>{html_module.escape(p)}</p>" for p in paragraphs)


def render_story_html(
    story: "Story", articles: list["Article"], primary: "Article | None"
) -> str:
    """Build the self-contained HTML document uploaded to Readeck.

    Readeck extracts this content (FindMain) and lists every outbound link in
    its Links panel — so all source article URLs are included in a Sources
    section.
    """
    esc = html_module.escape
    parts = [
        "<!DOCTYPE html><html><head>",
        '<meta charset="utf-8">',
        f"<title>{esc(story.title)}</title>",
        "</head><body>",
        "<main>",
        f"<h1>{esc(story.title)}</h1>",
    ]
    if story.image_url:
        parts.append(f'<img src="{esc(story.image_url, quote=True)}" alt="">')
    parts.append(_summary_to_html(story.summary))
    parts.append("<h2>Sources</h2><ul>")
    for a in articles:
        parts.append(
            f'<li><a href="{esc(a.url, quote=True)}">{esc(a.title or a.url)}</a></li>'
        )
    parts.append("</ul>")
    if primary is not None:
        parts.append(
            f'<p>Primary source: <a href="{esc(primary.url, quote=True)}">'
            f"{esc(primary.title or primary.url)}</a></p>"
        )
    parts.append("</main></body></html>")
    return "\n".join(parts)


async def _post_bookmark(
    *, url: str, title: str, html: str, labels: list[str], created: str | None
) -> dict[str, Any]:
    """Multipart POST to Readeck. Returns response headers (Bookmark-Id, Location)."""
    base = (settings.readeck_base_url or "").rstrip("/")
    data: dict[str, str] = {"url": url, "title": title}
    if created:
        data["created"] = created
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("html", ("story.html", html.encode("utf-8"), "text/html"))
    ]
    files += [("labels", (label, b"", "text/plain")) for label in labels]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/api/bookmarks",
                headers={"Authorization": f"Bearer {settings.readeck_token}"},
                data=data,
                files=files,
            )
    except httpx.HTTPError as exc:
        raise ReadeckError(f"Readeck request failed: {exc}") from exc
    if resp.status_code not in (200, 202):
        raise ReadeckError(
            f"Readeck returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    bookmark_id = resp.headers.get("bookmark-id", "")
    location = resp.headers.get("location", "")
    return {"bookmark_id": bookmark_id, "href": location or f"{base}/api/bookmarks/{bookmark_id}"}


async def save_story(
    story: "Story", articles: list["Article"]
) -> dict[str, Any]:
    """Render the story and push it to Readeck. Returns bookmark id + href."""
    if not is_enabled():
        raise ReadeckError("Readeck integration is not configured")
    if not articles:
        raise ReadeckError("Story has no articles")
    primary = next((a for a in articles if a.url == story.image_url), None)
    if primary is None:
        # lead article = earliest published, else first
        primary = min(
            articles,
            key=lambda a: (a.published_at is None, a.published_at or a.id),
        )
    labels = ["newsgator", story.category.lower()]
    created = (
        primary.published_at.isoformat() if primary.published_at is not None else None
    )
    start = time.monotonic()
    result = await _post_bookmark(
        url=primary.url,
        title=story.title,
        html=render_story_html(story, articles, primary),
        labels=labels,
        created=created,
    )
    result["latency_ms"] = int((time.monotonic() - start) * 1000)
    return result
