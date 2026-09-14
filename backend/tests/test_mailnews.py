"""Newsletter ingestion tests: extraction, IMAP poll, mail accounts API, story intro."""

import imaplib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import setup_admin

from app.core.config import settings
from app.models import Article, Feed, MailAccount, Story
from app.services import cluster, llm_client, mailnews
from app.services.vectorstore import InMemoryVectorStore

# Always computed relative to "now" — a fixed calendar date eventually falls
# outside the default backfill window (feed_backfill_days) and messages get
# silently skipped as "old" once enough real time has passed since this was
# written.
RECENT_DATE = format_datetime(datetime.now(UTC) - timedelta(hours=1))

# --- sample messages + fake IMAP ----------------------------------------------

NEWSLETTER_HTML = """
<html><body>
<p>Hello! <a href="https://example.com/email-unsubscribe?token=abc">Unsubscribe</a></p>
<ul>
<li>Un juge a annulé la mise à l'écart d'Anthropic par le Pentagone, jugée
arbitraire. (<a href="https://news.example.com/story-1?utm_source=nl">news</a>)</li>
<li><a href="https://blog.example.com/post-2">Un second article très complet</a>
qui parle de puces.</li>
</ul>
<p><a href="https://wa.me/?text=share">Partager sur WhatsApp</a></p>
<a href="https://img.example.com/banner"><img src="https://img.example.com/x.png"/></a>
</body></html>
"""


def make_message(
    sender: str = "Tech Café <news@techcafe.example>",
    subject: str = "Hebdo",
    date: str = RECENT_DATE,
    html: str = NEWSLETTER_HTML,
) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["Subject"] = subject
    m["Date"] = date
    m.set_content("Plain fallback with https://plain.example.com/item-1 inside.")
    m.add_alternative(html, subtype="html")
    return m.as_bytes()


class FakeIMAP:
    """Minimal imaplib.IMAP4 stand-in (login/select/uid SEARCH+FETCH/logout)."""

    def __init__(self, messages: dict[int, bytes], *, fail_login: bool = False):
        self.messages = messages
        self.fail_login = fail_login
        self.logged_in = False

    def login(self, username: str, password: str) -> None:
        if self.fail_login:
            raise imaplib.IMAP4.error("authentication failed")
        self.logged_in = True

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return ("OK", [b"1"])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            uids = b" ".join(str(u).encode() for u in sorted(self.messages))
            return ("OK", [uids])
        if command == "FETCH":
            uid = int(args[-2]) if len(args) >= 2 else int(args[0])
            raw = self.messages.get(uid)
            if raw is None:
                return ("NO", [b"not found"])
            return ("OK", [(f"{uid} (BODY[])".encode(), raw)])
        raise AssertionError(f"unexpected command {command}")

    def logout(self) -> None:
        pass


def _patch_imap(monkeypatch: pytest.MonkeyPatch, fake: FakeIMAP) -> None:
    monkeypatch.setattr(mailnews, "_imap_connect", lambda host, port, use_ssl: fake)


async def _stub_fulltext(session: AsyncSession, article: Article, feed: Feed) -> None:
    """Skip the network: mark full-text as done (state 'fulltext')."""
    article.processing_state = "fulltext"


async def _stub_fulltext_batch(feed_id: int, article_ids: list[int]) -> list[int]:
    """Batch seam: mark every article 'fulltext' without any network."""
    from app.core.db import get_session as _gs
    from app.models import Article as _A

    async for s in _gs():
        for aid in article_ids:
            art = await s.get(_A, aid)
            if art is not None:
                art.processing_state = "fulltext"
        await s.commit()
        break
    return list(article_ids)


def _no_refine(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM refinement off → code-heuristic intros."""
    monkeypatch.setattr(settings, "newsletter_llm_extract", False)


def _no_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM clean pass off (default stub: nothing filtered)."""
    monkeypatch.setattr(settings, "newsletter_llm_clean", False)


@pytest.fixture(autouse=True)
def _stub_clean_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clean pass is ON by default in settings; stub the free-text LLM call
    to return the body unchanged (nothing filtered) unless a test overrides it."""
    async def passthrough(system: str, user: str, model: str | None = None) -> tuple[str, int]:
        return user, 12

    monkeypatch.setattr(llm_client, "chat_text", passthrough)


@pytest.fixture(autouse=True)
def _clear_poll_locks() -> Iterator[None]:
    """The per-account poll lock is module state — never leak it across tests."""
    yield
    mailnews._polling_accounts.clear()


# --- pure extraction -----------------------------------------------------------


def test_extract_links_filters_junk() -> None:
    cands = mailnews.extract_links(NEWSLETTER_HTML)
    urls = [c.url for c in cands]
    assert "https://news.example.com/story-1" in urls  # utm param stripped
    assert "https://blog.example.com/post-2" in urls
    # junk filtered: unsubscribe, WhatsApp share, image-only banner link
    assert not any("unsubscribe" in u for u in urls)
    assert not any("wa.me" in u for u in urls)
    assert not any("img.example.com" in u for u in urls)


def test_parse_message_headers_and_links() -> None:
    msg = mailnews.parse_message(7, make_message())
    assert msg is not None
    assert msg.sender_email == "news@techcafe.example"
    assert msg.sender_name == "Tech Café"
    assert msg.subject == "Hebdo"
    assert msg.date is not None and msg.date.year == 2026
    assert len(msg.candidates) == 2


def test_parse_plain_text_fallback() -> None:
    m = EmailMessage()
    m["From"] = "Plain <plain@example.com>"
    m["Subject"] = "Links"
    m.set_content("Read https://plain.example.com/item-1 and https://plain.example.com/item-2")
    msg = mailnews.parse_message(3, m.as_bytes())
    assert msg is not None
    assert [c.url for c in msg.candidates] == [
        "https://plain.example.com/item-1",
        "https://plain.example.com/item-2",
    ]


def test_fallback_intro_uses_block_text() -> None:
    cands = mailnews.extract_links(NEWSLETTER_HTML)
    first = next(c for c in cands if "story-1" in c.url)
    intro = mailnews._fallback_intro(first)
    assert "Anthropic" in intro
    assert "(news)" not in intro  # anchor text removed


# --- polling -----------------------------------------------------------------


async def test_poll_creates_mail_feed_and_articles(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_refine(monkeypatch)
    _patch_imap(monkeypatch, FakeIMAP({10: make_message()}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p",
            folder="Newsletters",
        )
        s.add(account)
        await s.commit()
        result = await mailnews.poll_account(s, account)

    assert result == {"messages": 1, "new_articles": 2, "skipped_old": 0}
    async with db_session() as s:
        feed = await s.scalar(select(Feed).where(Feed.kind == "mail"))
        assert feed is not None
        assert feed.sender_email == "news@techcafe.example"
        assert feed.title == "Tech Café"
        assert feed.url == "newsletter:news@techcafe.example"
        assert feed.email_count == 1  # one message processed
        articles = (await s.scalars(select(Article).where(Article.feed_id == feed.id))).all()
        assert len(articles) == 2
        assert all(a.newsletter_intro for a in articles)
        assert all(a.processing_state == "fulltext" for a in articles)
        # account from the previous session block is detached — re-query
        refetched = await s.scalar(select(MailAccount))
        assert refetched is not None
        assert refetched.last_uid == 10
        assert refetched.last_error is None

        # Activity events emitted (invariant 6)
        from app.models import ActivityEvent

        actions = (
            await s.scalars(
                select(ActivityEvent.action).where(ActivityEvent.component == "mail")
            )
        ).all()
        assert "newsletter_feed_created" in actions
        assert "mail_poll_done" in actions


async def test_poll_account_skips_when_busy(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second poll (scheduler sweep while a manual poll is still processing)
    is a no-op: the watermark hasn't advanced, so it would redo the same work."""
    _no_refine(monkeypatch)
    _patch_imap(monkeypatch, FakeIMAP({10: make_message()}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p", folder="N"
        )
        s.add(account)
        await s.commit()
        assert mailnews.try_begin_poll(account.id) is True
        # Busy → skipped silently, IMAP never touched, watermark unchanged
        skipped = await mailnews.poll_account(s, account)
        assert skipped == {"messages": 0, "new_articles": 0, "skipped_old": 0}
        assert account.last_uid == 0
        mailnews.end_poll(account.id)
        # Released → the real poll goes through
        result = await mailnews.poll_account(s, account)
        assert result["messages"] == 1
        # ... and the lock is free again after processing (finally)
        assert account.id not in mailnews._polling_accounts


async def test_poll_llm_refinement_and_dedupe(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM intros win over heuristics; hallucinated URLs dropped; no reprocessing."""
    _patch_imap(monkeypatch, FakeIMAP({10: make_message(), 11: make_message()}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    async def fake_chat_json(
        system: str, user: str, model: str | None = None
    ) -> tuple[dict[str, Any], int]:
        return {
            "items": [
                {
                    "url": "https://news.example.com/story-1",
                    "title": "Anthropic vs le Pentagone",
                    "intro": "Intro humaine pour l'article un.",
                },
                # hallucinated URL — must be dropped
                {"url": "https://evil.example.com/nope", "title": "x", "intro": "y"},
            ]
        }, 42

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p", folder="INBOX"
        )
        s.add(account)
        await s.commit()
        # max 1 message per poll → second message processed on the next poll
        monkeypatch.setattr(settings, "mail_max_messages_per_poll", 1)
        first = await mailnews.poll_account(s, account)
        assert first["messages"] == 1
        await s.refresh(account)
        assert account.last_uid == 10
        second = await mailnews.poll_account(s, account)
        assert second["messages"] == 1
        third = await mailnews.poll_account(s, account)  # nothing new
        assert third == {"messages": 0, "new_articles": 0, "skipped_old": 0}

    async with db_session() as s:
        feed = await s.scalar(select(Feed).where(Feed.kind == "mail"))
        assert feed is not None
        assert feed.email_count == 2  # both messages processed, even though
        # their links deduped to the same 2 articles
        articles = (await s.scalars(select(Article).where(Article.feed_id == feed.id))).all()
        # 2 links per message, but the SAME message twice → URL dedupe on 2nd poll
        assert len(articles) == 2
        by_url = {a.url: a for a in articles}
        art1 = by_url["https://news.example.com/story-1"]
        assert art1.title == "Anthropic vs le Pentagone"
        assert art1.newsletter_intro == "Intro humaine pour l'article un."
        assert not any("evil.example.com" in a.url for a in articles)


async def test_extract_pass_maps_without_triage(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 2 does NO filtering (pass 1 owns that): a model still returning
    keep:false from habit must not drop the link."""
    _patch_imap(monkeypatch, FakeIMAP({10: make_message()}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    async def fake_chat_json(
        system: str, user: str, model: str | None = None
    ) -> tuple[dict[str, Any], int]:
        return {
            "items": [
                {
                    "url": "https://news.example.com/story-1",
                    "keep": False,  # stale triage field — must be ignored
                    "title": "Anthropic vs le Pentagone",
                    "intro": "Intro humaine.",
                },
            ]
        }, 42

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p", folder="INBOX"
        )
        s.add(account)
        await s.commit()
        result = await mailnews.poll_account(s, account)
        assert result["new_articles"] == 2  # both links kept

    async with db_session() as s:
        feed = await s.scalar(select(Feed).where(Feed.kind == "mail"))
        assert feed is not None
        urls = (await s.scalars(select(Article.url).where(Article.feed_id == feed.id))).all()
        assert "https://news.example.com/story-1" in urls


def test_self_referential_prefilter() -> None:
    """Patreon→patreon.com chrome is dropped by code, before the LLM."""
    html = """
    <html><body>
    <p><a href="https://www.patreon.com/posts/123?utm_campaign=x">View in app</a></p>
    <p><a href="https://www.patreon.com/posts/123">Like</a>
       <a href="https://www.patreon.com/creator/home">Comment</a></p>
    <p>Real story <a href="https://news.example.com/story">here</a> about stuff.</p>
    </body></html>
    """
    msg = mailnews.parse_message(1, make_message(html=html))
    assert msg is not None
    mailnews._drop_self_referential(msg)
    urls = [c.url for c in msg.candidates]
    assert not any("patreon.com" in u for u in urls)
    assert "https://news.example.com/story" in urls


# --- LLM clean pass (placeholder rendering + filtering) -----------------------


def test_render_with_placeholders() -> None:
    """Anchors become [text](«Ln») with a deduped URL map; junk URLs never
    appear in the rendered body (that's the point: no tracking-URL bloat)."""
    html = """
    <html><body>
    <p>Story one <a href="https://news.example.com/a?utm_source=nl">news</a> here.</p>
    <p>Same link again <a href="https://news.example.com/a">twice</a>, plus
       <a href="https://blog.example.com/b">another</a>.</p>
    <p><a href="https://img.example.com/banner"><img src="x.png"/></a></p>
    </body></html>
    """
    body, url_map = mailnews.render_with_placeholders(html)
    assert len(url_map) == 3  # dedupe by canonical URL; image-only link mapped
    assert url_map["«L0»"] == "https://news.example.com/a"
    # both anchors of the same URL reuse the same placeholder
    assert body.count("(«L0»)") == 2
    assert "utm_source" not in body
    assert "https://" not in body  # no real URL leaks into the prompt
    assert "[news](«L0»)" in body


async def test_clean_pass_filters_chrome_links(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM clean pass drops links whose paragraphs it deleted; unknown
    placeholders are ignored (no hallucinated URLs by construction)."""
    html = """
    <html><body>
    <p>Follow me on <a href="https://social.example.com/me">Social</a>!</p>
    <ul><li>Real news <a href="https://news.example.com/story-1">here</a>.</li>
    <li>More news <a href="https://blog.example.com/post-2">there</a>.</li></ul>
    </body></html>
    """
    _no_refine(monkeypatch)
    _patch_imap(monkeypatch, FakeIMAP({10: make_message(html=html)}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    async def fake_chat_text(system: str, user: str, model: str | None = None) -> tuple[str, int]:
        # the model deletes the social paragraph and garbles one token
        return "Real news [here](«L1»).\n\nMore news [there](«L2»). [«L99»]", 55

    monkeypatch.setattr(llm_client, "chat_text", fake_chat_text)

    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p", folder="INBOX"
        )
        s.add(account)
        await s.commit()
        result = await mailnews.poll_account(s, account)
        assert result["new_articles"] == 2

    async with db_session() as s:
        urls = (await s.scalars(select(Article.url))).all()
        assert "https://news.example.com/story-1" in urls
        assert "https://blog.example.com/post-2" in urls
        assert not any("social.example.com" in u for u in urls)

        from app.models import ActivityEvent, LLMUsage

        actions = (
            await s.scalars(
                select(ActivityEvent.action).where(ActivityEvent.component == "mail")
            )
        ).all()
        assert "newsletter_clean_done" in actions
        detail = await s.scalar(
            select(ActivityEvent.detail).where(
                ActivityEvent.action == "newsletter_clean_done"
            )
        )
        assert detail is not None and '"dropped": 1' in detail
        assert '"unknown_placeholders": 1' in detail
        # usage recorded with the dedicated kind
        kinds = (await s.scalars(select(LLMUsage.kind))).all()
        assert "newsletter_clean" in kinds


async def test_clean_pass_failure_keeps_all_candidates(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An LLM error on the clean pass must not lose a single link."""
    _no_refine(monkeypatch)
    _patch_imap(monkeypatch, FakeIMAP({10: make_message()}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    async def failing_chat_text(
        system: str, user: str, model: str | None = None
    ) -> tuple[str, int]:
        raise llm_client.LLMError("boom")

    monkeypatch.setattr(llm_client, "chat_text", failing_chat_text)

    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p", folder="INBOX"
        )
        s.add(account)
        await s.commit()
        result = await mailnews.poll_account(s, account)
        assert result["new_articles"] == 2  # both links of NEWSLETTER_HTML kept

    async with db_session() as s:
        from app.models import ActivityEvent

        actions = (
            await s.scalars(
                select(ActivityEvent.action).where(ActivityEvent.component == "mail")
            )
        ).all()
        assert "newsletter_clean_error" in actions


async def test_poll_records_error_and_keeps_watermark(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_imap(monkeypatch, FakeIMAP({5: make_message()}, fail_login=True))
    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p", folder="INBOX"
        )
        s.add(account)
        await s.commit()
        result = await mailnews.poll_account(s, account)
        assert result == {"messages": 0, "new_articles": 0, "skipped_old": 0}
        await s.refresh(account)
        assert "authentication failed" in (account.last_error or "")
        assert account.last_uid == 0  # whole-poll failure: watermark untouched


async def test_first_sync_backfill_window(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Messages older than feed_backfill_days are skipped on the FIRST sync only."""
    _no_refine(monkeypatch)
    monkeypatch.setattr(settings, "feed_backfill_days", 7)
    old = make_message(date="Sun, 01 Jan 2023 13:00:00 +0000")
    new = make_message(date=RECENT_DATE)
    _patch_imap(monkeypatch, FakeIMAP({1: old, 2: new}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    async with db_session() as s:
        account = MailAccount(
            user_id=1, host="imap.example.com", username="u", password="p", folder="INBOX"
        )
        s.add(account)
        await s.commit()
        result = await mailnews.poll_account(s, account)
        assert result["skipped_old"] == 1
        assert result["messages"] == 1
        await s.refresh(account)
        assert account.last_uid == 2  # skipped messages still advance the watermark


# --- story creation prefers the newsletter intro -------------------------------


async def test_new_story_prefers_newsletter_intro(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryVectorStore()
    monkeypatch.setattr(cluster, "get_vector_store", lambda session=None: store)

    async def fake_chat_json(
        system: str, user: str, model: str | None = None
    ) -> tuple[dict[str, Any], int]:
        return {"headline": "Un titre"}, 10

    async def fake_embed(texts: list[str], model: str | None = None) -> list[list[float]]:
        v = np.zeros(1024, dtype=np.float32)
        v[0] = 1.0
        return [v.tolist() for _ in texts]

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(llm_client, "embed", fake_embed)

    async with db_session() as s:
        feed = Feed(url="newsletter:n@x.example", kind="mail", sender_email="n@x.example")
        s.add(feed)
        await s.flush()
        article = Article(
            feed_id=feed.id,
            guid="g1",
            url="https://news.example.com/story-1",
            title="Article",
            summary="LLM summary in summary language.",
            newsletter_intro="Intro humaine de la newsletter.",
            processing_state="embedded",
        )
        s.add(article)
        await s.commit()
        await cluster.cluster_article(s, article.id)

        story = await s.scalar(select(Story))
        assert story is not None
        assert story.summary == "Intro humaine de la newsletter."  # human text wins
        assert article.story_id == story.id


# --- API ------------------------------------------------------------------------


async def test_mail_accounts_crud_and_isolation(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post(
        "/api/mail-accounts",
        json={
            "host": "imap.example.com",
            "username": "me@example.com",
            "password": "secret1",
            "folder": "Newsletters",
        },
    )
    assert r.status_code == 201, r.text
    acc = r.json()
    assert acc["port"] == 993 and acc["use_ssl"] is True
    assert "password" not in r.text  # write-only

    # folder is mandatory
    r = await client.post(
        "/api/mail-accounts",
        json={"host": "h", "username": "u", "password": "p", "folder": ""},
    )
    assert r.status_code == 422

    r = await client.get("/api/mail-accounts")
    assert [a["id"] for a in r.json()] == [acc["id"]]

    r = await client.patch(f"/api/mail-accounts/{acc['id']}", json={"folder": "INBOX.News"})
    assert r.status_code == 200 and r.json()["folder"] == "INBOX.News"

    # A second user does not see or touch the admin's account
    await client.post(
        "/api/users", json={"username": "reader", "password": "readerpass1"}
    )
    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"username": "reader", "password": "readerpass1"}
    )
    assert (await client.get("/api/mail-accounts")).json() == []
    assert (await client.get(f"/api/mail-accounts/{acc['id']}/test", params={})).status_code in (
        404,
        405,
    )
    assert (await client.delete(f"/api/mail-accounts/{acc['id']}")).status_code == 404

    # Anonymous is rejected
    await client.post("/api/auth/logout")
    assert (await client.get("/api/mail-accounts")).status_code == 401


async def test_mail_account_test_endpoint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    r = await client.post(
        "/api/mail-accounts",
        json={"host": "imap.example.com", "username": "u", "password": "p", "folder": "N"},
    )
    acc = r.json()

    _patch_imap(monkeypatch, FakeIMAP({}))
    r = await client.post(f"/api/mail-accounts/{acc['id']}/test")
    assert r.json()["ok"] is True

    _patch_imap(monkeypatch, FakeIMAP({}, fail_login=True))
    r = await client.post(f"/api/mail-accounts/{acc['id']}/test")
    body = r.json()
    assert body["ok"] is False
    assert body["errors"] and "authentication failed" in body["errors"][0]


async def test_poll_endpoint_conflicts_when_busy(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poll-now returns 409 while a poll is already running, and works again
    once it finished (lock released by the background task's finally)."""
    await setup_admin(client)
    r = await client.post(
        "/api/mail-accounts",
        json={"host": "imap.example.com", "username": "u", "password": "p", "folder": "N"},
    )
    acc = r.json()
    _patch_imap(monkeypatch, FakeIMAP({10: make_message()}))
    monkeypatch.setattr(mailnews, "_fulltext_batch", _stub_fulltext_batch)

    assert mailnews.try_begin_poll(acc["id"]) is True
    r = await client.post(f"/api/mail-accounts/{acc['id']}/poll")
    assert r.status_code == 409
    mailnews.end_poll(acc["id"])

    # Free → poll runs (inline under ENVIRONMENT=test) and releases the lock
    r = await client.post(f"/api/mail-accounts/{acc['id']}/poll")
    assert r.status_code == 202, r.text
    assert r.json()["found"] == 1
    assert acc["id"] not in mailnews._polling_accounts

    # Watermark advanced: a follow-up poll finds nothing (and doesn't 409)
    r = await client.post(f"/api/mail-accounts/{acc['id']}/poll")
    assert r.status_code == 202 and r.json()["found"] == 0


async def test_refresh_rejects_mail_feed(
    client: AsyncClient, db_session: async_sessionmaker[AsyncSession]
) -> None:
    await setup_admin(client)
    async with db_session() as s:
        feed = Feed(url="newsletter:n@x.example", kind="mail", sender_email="n@x.example")
        s.add(feed)
        await s.commit()
        feed_id = feed.id
    r = await client.post(f"/api/feeds/{feed_id}/refresh")
    assert r.status_code == 400
    # but it is listed like any feed
    r = await client.get("/api/feeds")
    listed = {f["id"]: f for f in r.json()}
    assert listed[feed_id]["kind"] == "mail"
    assert listed[feed_id]["sender_email"] == "n@x.example"
