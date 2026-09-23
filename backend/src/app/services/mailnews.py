"""Newsletter ingestion via per-user IMAP accounts (SPEC §9, mail path).

Flow per enabled account (scheduler every MAIL_POLL_MINUTES):

1. IMAP login, SELECT the configured folder READ-ONLY (messages are never
   flagged \\Seen) and fetch messages with UID > last_uid, oldest first,
   capped at MAIL_MAX_MESSAGES_PER_POLL.
2. Each message's sender (From:) maps to a mail Feed (kind='mail'), created on
   first sight with the display name as title (event newsletter_feed_created).
3. Article links are extracted CODE-FIRST: lxml parses the HTML body, junk
   links (unsubscribe, share intents, tracking, empty anchors) are filtered and
   URLs canonicalized. Then TWO LLM passes refine the selection:
   pass 1 (NEWSLETTER_LLM_CLEAN) shows the model the full email with
   placeholder link tokens (never the real URLs) and lets it DELETE the
   non-news chrome (intro, socials/footer, sponsors, platform self-links) —
   surviving placeholders map back to candidate URLs; pass 2
   (NEWSLETTER_LLM_EXTRACT) is pure text mapping: each surviving link gets a
   title + human-written intro — validated against the code-extracted URL set
   so hallucinated URLs are dropped; heuristic block-text fallback at every
   failure.
4. Each link becomes an Article on the mail feed (newsletter_intro set) and
   goes through the standard fulltext → summarize → embed → cluster pipeline.
5. account.last_uid advances per processed message (resumability, invariant 7).

All blocking IMAP work runs via anyio.to_thread; no DB writes are held across
network I/O (same writer-lock discipline as ingest.py).
"""

import asyncio
import imaplib
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import parseaddr, parsedate_to_datetime

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from app.core.config import settings
from app.core.db import get_session
from app.models import Article, Feed, MailAccount, UserFeed
from app.services import activity, llm_client, llmtrace, prompts, usage
from app.services.fulltext import fetch_full_text_batch
from app.services.ingest import _is_duplicate, canonicalize_url

# --- code-first link extraction ----------------------------------------------

# Junk filters: newsletter chrome, not articles. Applied to anchor text AND href.
_JUNK_RE = re.compile(
    r"unsubscribe|d[ée]sabonn|manage (your )?(email )?(preferences|settings)"
    r"|view (this|in|on) (browser|app|web)|privacy policy|terms of (use|service)"
    r"|update (your )?preferences|email(-| )settings",
    re.IGNORECASE,
)
_SHARE_RE = re.compile(
    r"^(https?://)?(wa\.me|api\.whatsapp\.com|(www\.)?(facebook\.com/(sharer|share)"
    r"|(twitter|x)\.com/intent|linkedin\.com/share))",
    re.IGNORECASE,
)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


@dataclass
class LinkCandidate:
    url: str  # canonicalized
    anchor: str
    context: str  # enclosing block text (intro fallback)


@dataclass
class NewsletterMessage:
    uid: int
    sender_email: str
    sender_name: str
    subject: str
    date: datetime | None
    html: str
    candidates: list[LinkCandidate] = field(default_factory=list)


def extract_links(html_text: str) -> list[LinkCandidate]:
    """Code-first link extraction from a newsletter HTML body.

    Keeps only http(s) links with a non-empty anchor, drops junk/share/tracking
    links, dedupes by canonical URL. `context` (the enclosing block's text) is
    the heuristic intro fallback and the grounding for the LLM refinement.
    """
    from lxml import html as lxml_html  # transitive dep (trafilatura)

    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return []
    out: list[LinkCandidate] = []
    seen: set[str] = set()
    for a in doc.iter("a"):
        href = (a.get("href") or "").strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        anchor = " ".join(a.text_content().split())
        if not anchor:  # image-only links (banners, logo buttons)
            continue
        if _JUNK_RE.search(anchor) or _JUNK_RE.search(href) or _SHARE_RE.search(href):
            continue
        url = canonicalize_url(href)
        if url in seen:
            continue
        seen.add(url)
        block: lxml_html.HtmlElement | None = a
        while block is not None and block.tag not in ("p", "li", "td", "div", "body"):
            block = block.getparent()
        context = (
            " ".join(block.text_content().split())[:600] if block is not None else anchor
        )
        out.append(LinkCandidate(url=url, anchor=anchor[:300], context=context))
    return out


def _host(url: str) -> str:
    """Bare host (no port), lowercased, with a leading www. stripped."""
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _drop_self_referential(msg: "NewsletterMessage") -> None:
    """Cheap deterministic pre-filter: links pointing back at the newsletter's
    OWN platform (Patreon→patreon.com, Substack→substack.com, …) are never
    external news — they are the platform's like/comment/share/view-in-app
    chrome. Detected from the sender domain when the host is a known
    newsletter platform, else from the most common link host (the platform's
    own links dominate the chrome). Runs BEFORE the LLM so the model only
    triages genuinely ambiguous links.
    """
    from collections import Counter

    platform_hosts = {
        "patreon.com",
        "substack.com",
        "mailchimp.com",
        "beehiiv.com",
        "buttondown.email",
        "convertkit.com",
        "sendinblue.com",
    }
    if not msg.candidates:
        return
    counts = Counter(_host(c.url) for c in msg.candidates)
    top_host, top_n = counts.most_common(1)[0]
    # A platform self-host is either a known newsletter platform that appears
    # repeatedly, or simply the dominant host when it clearly outnumbers the rest
    # (the platform's chrome links outnumber the curated news links).
    is_platform = top_host in platform_hosts or (
        top_n >= 3 and top_n > len(msg.candidates) // 2
    )
    if not is_platform:
        return
    kept = [c for c in msg.candidates if _host(c.url) != top_host]
    dropped = len(msg.candidates) - len(kept)
    if dropped:
        msg.candidates = kept


def _fallback_intro(candidate: LinkCandidate) -> str:
    """Heuristic intro: enclosing block text minus the anchor itself."""
    intro = candidate.context.replace(candidate.anchor, " ")
    # strip leftover list markers/separators (incl. unicode bullets/dashes)
    return " ".join(intro.split()).strip(" \u2013\u2014\u2022\u00b7-")[:2000]


# --- LLM clean pass (step 1 of the two-pass pipeline) -------------------------

# Placeholder link tokens («L0», «L1», …): the LLM judges a link's role from its
# anchor text and surrounding words — it never needs the real URL, which is
# hundreds of chars of tracking junk that bloats the prompt (measured: -50%
# prompt tokens, -50% latency vs. sending full URLs).
_PLACEHOLDER_RE = re.compile(r"\u00abL\d+\u00bb")
_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td"}


def render_with_placeholders(html_text: str) -> tuple[str, dict[str, str]]:
    """Render the newsletter HTML as text blocks with placeholder links.

    Returns (body_text, {placeholder: canonical_url}). Each anchor becomes
    "[visible text](«L42»)" — «L42» dedupes by canonical URL. Image-only
    (anchor-less) links are dropped from the text but stay in the map so a
    surviving placeholder never counts as hallucinated.
    """
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return "", {}
    url_map: dict[str, str] = {}

    def _placeholder(href: str) -> str:
        canon = canonicalize_url(href)
        for p, u in url_map.items():
            if u == canon:
                return p
        p = f"\u00abL{len(url_map)}\u00bb"
        url_map[p] = canon
        return p

    for a in doc.iter("a"):
        href = (a.get("href") or "").strip()
        text = " ".join(a.text_content().split())
        for child in list(a):
            a.remove(child)
        if href.lower().startswith(("http://", "https://")):
            p = _placeholder(href)
            a.text = f"[{text}]({p})" if text else ""
        else:
            a.text = text
    lines: list[str] = []
    for el in doc.iter():
        if el.tag in _BLOCK_TAGS and not any(c.tag in _BLOCK_TAGS for c in el):
            t = " ".join(el.text_content().split())
            if t:
                lines.append(t)
    return "\n\n".join(lines), url_map


async def _llm_clean_filter(
    session: AsyncSession, msg: "NewsletterMessage"
) -> set[str] | None:
    """LLM pass 1: delete the non-news chrome from the full email, return the
    set of candidate URLs that SURVIVED. None = pass unavailable/failed/off
    (caller keeps all candidates — better to over-include than lose news).

    Output validation is by construction: only placeholders that were handed
    to the model can come back, and the result is intersected with the
    code-extracted candidate set, so hallucinated/garbled URLs are impossible.
    """
    if not settings.newsletter_llm_clean or not msg.candidates:
        return None
    sender = msg.sender_name or msg.sender_email
    body, url_map = render_with_placeholders(msg.html)
    if not body or not url_map:
        return None
    await activity.emit(
        session,
        "mail",
        "newsletter_clean_start",
        {"sender": sender, "subject": msg.subject, "chars": len(body)},
    )
    await session.commit()
    try:
        system, user = prompts.newsletter_clean(msg.subject, body)
        with llmtrace.context("newsletter_clean", label=f"clean: {sender}: {msg.subject}"):
            cleaned, latency_ms = await llm_client.chat_text(system, user)
    except llm_client.LLMError as exc:
        await activity.emit(
            session,
            "mail",
            "newsletter_clean_error",
            {"sender": sender, "error": str(exc)[:300]},
            level="error",
        )
        await session.commit()
        return None
    surviving = dict.fromkeys(_PLACEHOLDER_RE.findall(cleaned))  # ordered set
    valid = {c.url for c in msg.candidates}
    kept = {url_map[p] for p in surviving if p in url_map} & valid
    unknown = len([p for p in surviving if p not in url_map])
    usage.record(
        session,
        "newsletter_clean",
        endpoint="chat",
        model=settings.llm_model,
        latency_ms=latency_ms,
        prompt_chars=len(system) + len(user),
        completion_chars=len(cleaned),
    )
    await activity.emit(
        session,
        "mail",
        "newsletter_clean_done",
        {
            "sender": sender,
            "subject": msg.subject,
            "links": len(msg.candidates),
            "kept": len(kept),
            "dropped": len(valid - kept),
            "unknown_placeholders": unknown,
            "llm_ms": latency_ms,
        },
    )
    await session.commit()
    return kept


def parse_message(uid: int, raw: bytes) -> NewsletterMessage | None:
    """Parse one RFC822 message. Returns None when there is nothing usable."""
    try:
        msg = BytesParser(policy=email_policy).parsebytes(raw)
    except Exception:
        return None
    sender_name, sender_email = parseaddr(str(msg.get("From", "")))
    if not sender_email or "@" not in sender_email:
        return None
    subject = str(make_header(decode_header(msg.get("Subject") or "")))[:300]
    date: datetime | None = None
    raw_date = msg.get("Date")
    if raw_date:
        try:
            dt = parsedate_to_datetime(str(raw_date))
            date = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            date = None
    html = _body_html(msg)
    if not html:
        return None
    parsed = NewsletterMessage(
        uid=uid,
        sender_email=sender_email.lower(),
        sender_name=sender_name.strip(),
        subject=subject,
        date=date,
        html=html,
    )
    parsed.candidates = extract_links(html)
    _drop_self_referential(parsed)
    return parsed


def _body_html(msg: EmailMessage) -> str:
    """Prefer text/html; fall back to linkified text/plain (bare-URL newsletters)."""
    plain = ""
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/html":
            try:
                return str(part.get_content())
            except Exception:
                continue
        if ctype == "text/plain" and not plain:
            try:
                plain = str(part.get_content())
            except Exception:
                pass
    if not plain:
        return ""
    # No HTML part: wrap plain text so the anchor parser sees the URLs
    links = "".join(
        f'<p><a href="{u}">{u}</a></p>' for u in dict.fromkeys(_BARE_URL_RE.findall(plain))
    )
    return links


# --- IMAP (blocking, runs in a thread) ---------------------------------------

# Never let a stalled IMAP server wedge a request/task forever: imaplib
# defaults to the GLOBAL socket timeout (None = infinite). The GUI "Poll now"
# button greys until the endpoint answers, so the search phase must always
# come back — timeout or result. Applies to connect AND every command/read.
IMAP_TIMEOUT_S = 30.0


class _TimeoutIMAP4(imaplib.IMAP4):
    """IMAP4 whose every socket op uses IMAP_TIMEOUT_S.

    `file` is a read-only property in imaplib — never assign it. For plain
    sockets makefile() only does a dup() (no buffering issue), so we simply
    skip creating it here.
    """

    def __init__(self, host: str, port: int):
        # IMAP4.__init__ passes timeout through to open() — hand ours in,
        # otherwise it would pass None (= infinite, the bug we're fixing).
        super().__init__(host, port, timeout=IMAP_TIMEOUT_S)

    def open(
        self, host: str = "", port: int = 143, timeout: float | None = IMAP_TIMEOUT_S
    ) -> None:
        self.host, self.port = host, port
        self.sock = socket.create_connection((host, port), timeout=timeout)


class _TimeoutIMAP4SSL(imaplib.IMAP4_SSL):
    """IMAP4_SSL with a real socket timeout (stdlib handles TLS + makefile)."""

    def __init__(self, host: str, port: int):
        super().__init__(host, port, timeout=IMAP_TIMEOUT_S)


def _imap_connect(host: str, port: int, use_ssl: bool) -> imaplib.IMAP4:
    """Module-level seam for tests (monkeypatched)."""
    if use_ssl:
        return _TimeoutIMAP4SSL(host, port)
    return _TimeoutIMAP4(host, port)


def _uid_search_new(imap: imaplib.IMAP4, last_uid: int, limit: int) -> list[int]:
    """UIDs strictly above the watermark, ascending, capped."""
    typ, data = imap.uid("SEARCH", None, "ALL")  # type: ignore[arg-type]
    if typ != "OK" or not data or not data[0]:
        return []
    uids = [int(u) for u in data[0].split()]
    return [u for u in uids if u > last_uid][:limit]


def _search_new_uids(account: MailAccount) -> list[int]:
    """Search phase only: login + folder + UIDs above the watermark. FAST — this
    is what the 'Poll now' endpoint waits on."""
    imap = _imap_connect(account.host, account.port, account.use_ssl)
    try:
        imap.login(account.username, account.password)
        imap.select(f'"{account.folder}"', readonly=True)
        return _uid_search_new(imap, account.last_uid, settings.mail_max_messages_per_poll)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _fetch_raw_by_uid(account: MailAccount, uids: list[int]) -> list[tuple[int, bytes]]:
    """Fetch phase: download full bodies (BODY.PEEK — never sets \\Seen). The
    potentially slow part; runs in the background after the count is known."""
    if not uids:
        return []
    imap = _imap_connect(account.host, account.port, account.use_ssl)
    try:
        imap.login(account.username, account.password)
        imap.select(f'"{account.folder}"', readonly=True)
        out: list[tuple[int, bytes]] = []
        for uid in uids:
            typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            out.append((uid, bytes(data[0][1])))
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


class ProbeResult(TypedDict):
    ok: bool
    errors: list[str]
    folder: str | None


async def test_account(account: MailAccount) -> ProbeResult:
    """Probe for the GUI 'Test connection' button: login + folder exists."""
    try:
        await anyio.to_thread.run_sync(_probe_account, account)
    except Exception as exc:
        return {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"], "folder": None}
    return {"ok": True, "errors": [], "folder": account.folder}


def _probe_account(account: MailAccount) -> None:
    imap = _imap_connect(account.host, account.port, account.use_ssl)
    try:
        imap.login(account.username, account.password)
        typ, _ = imap.select(f'"{account.folder}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"folder not found: {account.folder}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# --- feed + article creation ---------------------------------------------------


async def get_or_create_mail_feed(
    session: AsyncSession, sender_email: str, sender_name: str
) -> tuple[Feed, bool]:
    """One mail feed per sender address (SPEC §9). Title = From display name."""
    pseudo_url = f"newsletter:{sender_email}"
    feed = await session.scalar(select(Feed).where(Feed.url == pseudo_url))
    if feed is not None:
        if sender_name and not feed.title:
            feed.title = sender_name
        return feed, False
    feed = Feed(
        url=pseudo_url,
        kind="mail",
        sender_email=sender_email,
        title=sender_name or sender_email,
    )
    session.add(feed)
    await session.flush()
    await activity.emit(
        session,
        "mail",
        "newsletter_feed_created",
        {"feed_id": feed.id, "sender": sender_email, "title": feed.title},
    )
    return feed, True


EXTRACT_BATCH_SIZE = 12


async def _refine_with_llm(
    session: AsyncSession,
    msg: NewsletterMessage,
    candidates: list[LinkCandidate],
) -> dict[str, tuple[str, str]]:
    """Batch LLM calls: (title, verbatim intro) per link. Pure text mapping —
    filtering is pass 1's job (_llm_clean_filter).

    Candidates are sliced into chunks of EXTRACT_BATCH_SIZE so prompt and
    completion sizes stay well within context windows and network timeouts, and
    a failure in one chunk doesn't forfeit the others.

    URLs are validated against the code-extracted candidate set — hallucinated
    URLs are dropped. On LLM failure for a batch, falls back to heuristic intros
    for that batch's items (better to over-include than lose news).
    """
    if not settings.newsletter_llm_extract or not candidates:
        return {}
    sender = msg.sender_name or msg.sender_email
    await activity.emit(
        session,
        "mail",
        "newsletter_extract_start",
        {"sender": sender, "subject": msg.subject, "links": len(candidates)},
    )
    await session.commit()

    valid = {c.url for c in candidates}
    out: dict[str, tuple[str, str]] = {}
    total_latency_ms = 0
    num_batches = (len(candidates) + EXTRACT_BATCH_SIZE - 1) // EXTRACT_BATCH_SIZE

    for batch_idx in range(num_batches):
        chunk = candidates[batch_idx * EXTRACT_BATCH_SIZE : (batch_idx + 1) * EXTRACT_BATCH_SIZE]
        batch_label = (
            f"extract: {sender}: {msg.subject}"
            if num_batches == 1
            else f"extract ({batch_idx + 1}/{num_batches}): {sender}: {msg.subject}"
        )
        try:
            system, user = prompts.newsletter_extract(
                sender, msg.subject, [(c.url, c.anchor, c.context) for c in chunk]
            )
            with llmtrace.context("newsletter_extract", label=batch_label):
                result, latency_ms = await llm_client.chat_json(system, user)
            total_latency_ms += latency_ms
            usage.record(
                session,
                "newsletter_extract",
                endpoint="chat",
                model=settings.llm_model,
                latency_ms=latency_ms,
                prompt_chars=len(system) + len(user),
            )
            items = result.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = canonicalize_url(str(item.get("url", "")))
                    if url not in valid:
                        continue
                    out[url] = (
                        str(item.get("title", ""))[:300],
                        str(item.get("intro", ""))[:2000],
                    )
        except llm_client.LLMError as exc:
            await activity.emit(
                session,
                "mail",
                "newsletter_extract_error",
                {
                    "sender": sender,
                    "batch": f"{batch_idx + 1}/{num_batches}",
                    "error": str(exc)[:300],
                },
                level="error",
            )
            await session.commit()

    await activity.emit(
        session,
        "mail",
        "newsletter_extract_done",
        {
            "sender": sender,
            "links": len(candidates),
            "extracted": len(out),
            "llm_ms": total_latency_ms,
        },
    )
    await session.commit()
    return out


async def _fulltext_batch(feed_id: int, article_ids: list[int]) -> list[int]:
    """Module-level seam so tests stub the network fetch (monkeypatched)."""
    return await fetch_full_text_batch(feed_id, article_ids)


async def _process_message(
    session: AsyncSession, msg: NewsletterMessage, user_id: int | None = None
) -> int:
    """Create articles for one parsed newsletter. Returns new article count."""
    if not msg.candidates:
        return 0
    feed, _created = await get_or_create_mail_feed(
        session, msg.sender_email, msg.sender_name
    )
    feed.email_count += 1
    if user_id is not None:
        uf = await session.scalar(
            select(UserFeed).where(
                UserFeed.user_id == user_id, UserFeed.feed_id == feed.id
            )
        )
        if uf is None:
            session.add(UserFeed(user_id=user_id, feed_id=feed.id))
    await session.commit()  # clean session before the LLM call (writer-lock rule)

    # Pass 1 (optional): the LLM deletes the non-news chrome from the full
    # email (placeholder links), so pass 2 only sees real news candidates.
    kept = await _llm_clean_filter(session, msg)
    candidates = msg.candidates if kept is None else [c for c in msg.candidates if c.url in kept]

    refined = await _refine_with_llm(session, msg, candidates)
    await session.commit()  # persist the usage row before any article writes

    new_ids: list[int] = []
    fulltext_pending: list[int] = []
    for cand in candidates:
        if await _is_duplicate(session, feed, f"{msg.uid}:{cand.url}", cand.url):
            continue
        title, intro = refined.get(cand.url, ("", ""))
        if not intro:
            intro = _fallback_intro(cand)
        article = Article(
            feed_id=feed.id,
            guid=f"mail:{msg.uid}:{cand.url}",
            url=cand.url,
            title=title or cand.anchor,
            raw_content=intro,  # fallback text if full-text fetch fails
            newsletter_intro=intro or None,
            published_at=msg.date,
            processing_state="fetched",
        )
        session.add(article)
        await session.flush()
        if feed.fetch_fulltext:
            fulltext_pending.append(article.id)
        else:
            article.processing_state = "fulltext"
            new_ids.append(article.id)
    await session.commit()
    # Full-text AFTER the batch commit. Run the fetches CONCURRENTLY (bounded):
    # a 50+ link newsletter fetched serially (direct → archive.is, 30s timeouts
    # each + 2s/domain delays) would take tens of minutes before the LLM queue
    # even starts filling. The batch helper uses its own session per article and
    # keeps the per-domain rate limit, so concurrency stays polite.
    fetched = await _fulltext_batch(feed.id, fulltext_pending)
    new_ids.extend(fetched)
    if new_ids:
        from app.services.process import enqueue_article

        for article_id in new_ids:
            enqueue_article(article_id)
    return len(new_ids)


# --- polling --------------------------------------------------------------------


@dataclass
class FetchedMail:
    """One raw message fetched from IMAP, not yet parsed/processed."""

    uid: int
    raw: bytes


async def search_new_uids(account: MailAccount) -> list[int]:
    """Async wrapper: UIDs above the watermark (fast search phase only)."""
    return await anyio.to_thread.run_sync(_search_new_uids, account)


async def fetch_new_messages(account: MailAccount) -> list[FetchedMail]:
    """Fetch raw bytes of messages above the watermark (search + bodies).

    Split from processing so the API can report 'N messages found' immediately
    and process in the background. Raises on IMAP errors (caller handles).
    """
    uids = await search_new_uids(account)
    raw_messages = await anyio.to_thread.run_sync(_fetch_raw_by_uid, account, uids)
    return [FetchedMail(uid=uid, raw=raw) for uid, raw in raw_messages]


async def fetch_messages_by_uid(
    account: MailAccount, uids: list[int]
) -> list[FetchedMail]:
    """Bodies for KNOWN uids — the background half of the 'Poll now' split."""
    raw_messages = await anyio.to_thread.run_sync(_fetch_raw_by_uid, account, uids)
    return [FetchedMail(uid=uid, raw=raw) for uid, raw in raw_messages]


async def process_messages(
    session: AsyncSession, account: MailAccount, messages: list[FetchedMail]
) -> dict[str, int]:
    """Parse + process already-fetched messages, advancing the UID watermark.

    Emits per-message events so the Activity page shows progress. Never raises
    for a single message; whole-batch IMAP failures happen in fetch_new_messages.
    """
    total = len(messages)
    # First-sync backfill window (feed_backfill_days, 0 = everything): skip
    # messages older than the window — but still advance the watermark past them.
    cutoff: datetime | None = None
    if account.last_uid == 0 and settings.feed_backfill_days > 0:
        cutoff = datetime.now(UTC) - timedelta(days=settings.feed_backfill_days)

    new_articles = 0
    skipped_old = 0
    processed = 0
    account_id = account.id
    for index, mail in enumerate(messages, start=1):
        msg = parse_message(mail.uid, mail.raw)
        # Poison/unusable messages still advance the watermark (they would fail
        # forever); the watermark only stalls on whole-poll IMAP failures.
        if msg is not None:
            if cutoff is not None and msg.date is not None and msg.date < cutoff:
                skipped_old += 1
            else:
                try:
                    sender = msg.sender_name or msg.sender_email
                    await activity.emit(
                        session,
                        "mail",
                        "newsletter_processing",
                        {
                            "uid": msg.uid,
                            "sender": sender,
                            "subject": msg.subject,
                            "links": len(msg.candidates),
                            "position": f"{index}/{total}",
                        },
                    )
                    await session.commit()
                    new_articles += await _process_message(session, msg, user_id=account.user_id)
                    processed += 1
                except Exception as exc:
                    await session.rollback()
                    # rollback expired the ORM objects — re-attach the account
                    fresh = await session.get(MailAccount, account_id)
                    if fresh is not None:
                        account = fresh
                    await activity.emit(
                        session,
                        "mail",
                        "newsletter_message_error",
                        {"uid": mail.uid, "error": str(exc)[:500]},
                        level="error",
                    )
        account.last_uid = max(account.last_uid, mail.uid)
        # Persist the watermark per message: a crash mid-batch never reprocesses
        await session.commit()

    account.last_checked_at = datetime.now(UTC)
    account.last_error = None
    await activity.emit(
        session,
        "mail",
        "mail_poll_done",
        {
            "account": f"{account.username}@{account.host}",
            "folder": account.folder,
            "messages": processed,
            "new_articles": new_articles,
            "skipped_old": skipped_old,
        },
    )
    await session.commit()
    return {"messages": processed, "new_articles": new_articles, "skipped_old": skipped_old}


# --- poll serialization --------------------------------------------------------

# One poll at a time per account. The GUI "Poll now" button re-enables as soon
# as the search phase answers (seconds) while body download + LLM processing
# run in the background for minutes — a second click, or the scheduler sweep
# firing mid-poll, would reprocess the SAME messages (the UID watermark only
# advances as messages complete), doubling every LLM call and racing the
# dedupe check. Single event loop → dict lookup is race-free.
# Locks auto-expire after POLL_LOCK_TIMEOUT_S to guarantee self-healing if a
# task crashes or wedges.
POLL_LOCK_TIMEOUT_S = 1800.0  # 30 minutes
_polling_accounts: dict[int, float] = {}


def try_begin_poll(account_id: int) -> bool:
    """Mark an account as being polled; False when a poll is already running."""
    now = time.monotonic()
    started = _polling_accounts.get(account_id)
    if started is not None and (now - started) < POLL_LOCK_TIMEOUT_S:
        return False
    _polling_accounts[account_id] = now
    return True


def end_poll(account_id: int) -> None:
    _polling_accounts.pop(account_id, None)


async def poll_account(session: AsyncSession, account: MailAccount) -> dict[str, int]:
    """Poll one IMAP account (fetch + process). Never raises."""
    if not try_begin_poll(account.id):
        # A manual "Poll now" (or the previous sweep) is still running — the
        # watermark is unchanged, so polling now would redo the same work.
        return {"messages": 0, "new_articles": 0, "skipped_old": 0}
    try:
        async with asyncio.timeout(POLL_LOCK_TIMEOUT_S):
            await activity.emit(
                session,
                "mail",
                "mail_poll_start",
                {"account": f"{account.username}@{account.host}", "folder": account.folder},
            )
            await session.commit()
            try:
                messages = await fetch_new_messages(account)
            except Exception as exc:
                account.last_checked_at = datetime.now(UTC)
                account.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                await activity.emit(
                    session,
                    "mail",
                    "mail_poll_failed",
                    {"account": f"{account.username}@{account.host}", "error": account.last_error},
                    level="error",
                )
                await session.commit()
                return {"messages": 0, "new_articles": 0, "skipped_old": 0}
            return await process_messages(session, account, messages)
    except TimeoutError:
        account.last_checked_at = datetime.now(UTC)
        account.last_error = f"Poll timed out after {int(POLL_LOCK_TIMEOUT_S // 60)} minutes"
        await activity.emit(
            session,
            "mail",
            "mail_poll_failed",
            {"account": f"{account.username}@{account.host}", "error": account.last_error},
            level="error",
        )
        await session.commit()
        return {"messages": 0, "new_articles": 0, "skipped_old": 0}
    finally:
        end_poll(account.id)


async def poll_all_accounts() -> None:
    """Scheduler entry point: poll every enabled account, failures isolated."""
    async for session in get_session():
        accounts = (
            await session.scalars(select(MailAccount).where(MailAccount.is_enabled))
        ).all()
        for account in accounts:
            await poll_account(session, account)
        break
