# ruff: noqa: E501
"""mailnews edge cases: parsing fallbacks, IMAP phases, timeouts and failure isolation."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import MailAccount
from app.services import mailnews


@pytest.fixture(autouse=True)
def _clear_locks() -> Iterator[None]:
    mailnews._polling_accounts.clear()
    yield
    mailnews._polling_accounts.clear()


class FakeConn:
    def __init__(
        self,
        uids: bytes | None = b"1 2 3",
        select_typ: str = "OK",
        fetch: dict[int, Any] | None = None,
        search_typ: str = "OK",
        logout_fails: bool = False,
    ) -> None:
        self.uids, self.select_typ, self.fetch = uids, select_typ, fetch or {}
        self.search_typ, self.logout_fails = search_typ, logout_fails
        self.selected: list[str] = []

    def login(self, u: str, p: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected.append(folder)
        return self.select_typ, [b"1"]

    def uid(self, cmd: str, *args: Any) -> tuple[str, list[Any]]:
        if cmd == "SEARCH":
            return self.search_typ, [self.uids] if self.uids is not None else []
        return self.fetch[int(args[0])]

    def logout(self) -> None:
        if self.logout_fails:
            raise OSError("closed")


def _account(**kw: Any) -> MailAccount:
    base: dict[str, Any] = {
        "user_id": 1,
        "host": "imap.x",
        "username": "u",
        "password": "p",
        "folder": "INBOX",
    }
    return MailAccount(**{**base, **kw})


def _use(monkeypatch: pytest.MonkeyPatch, conn: FakeConn) -> None:
    monkeypatch.setattr(mailnews, "_imap_connect", lambda h, p, s: conn)


def test_extract_links_survives_unparsable_html_and_skips_non_http() -> None:
    assert mailnews.extract_links("") == []
    html = (
        '<p><a href="mailto:a@b.c">mail me</a> <a href="/rel">rel</a>'
        ' <a href="https://a.example/x">First</a> <a href="https://a.example/x">Dup</a></p>'
    )
    links = mailnews.extract_links(html)
    assert [c.anchor for c in links] == ["First"]


def test_render_with_placeholders_unparsable_and_non_http_links() -> None:
    assert mailnews.render_with_placeholders("") == ("", {})
    body, url_map = mailnews.render_with_placeholders(
        '<p><a href="mailto:x@y.z">write us</a> and <a href="https://a.example/p">Read</a></p>'
    )
    assert "write us" in body and "mailto" not in body
    assert list(url_map.values()) == ["https://a.example/p"]


def test_drop_self_referential_noop_without_candidates() -> None:
    msg = mailnews.NewsletterMessage(
        uid=1, sender_email="a@b.c", sender_name="", subject="s", date=None, html="", candidates=[]
    )
    mailnews._drop_self_referential(msg)
    assert msg.candidates == []


async def test_llm_clean_filter_noops(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    link = mailnews.LinkCandidate(url="https://a.example/x", anchor="A", context="c")
    msg = mailnews.NewsletterMessage(
        uid=1,
        sender_email="a@b.c",
        sender_name="",
        subject="s",
        date=None,
        html="<p>no links</p>",
        candidates=[link],
    )
    async with db_session() as s:
        monkeypatch.setattr(mailnews.settings, "newsletter_llm_clean", False)
        assert await mailnews._llm_clean_filter(s, msg) is None
        monkeypatch.setattr(mailnews.settings, "newsletter_llm_clean", True)
        assert await mailnews._llm_clean_filter(s, msg) is None  # body has no URL placeholders


def test_parse_message_rejects_bad_sender_and_bodyless() -> None:
    assert mailnews.parse_message(1, b"Subject: x\r\n\r\nbody") is None  # no From
    assert mailnews.parse_message(1, b"From: nobody\r\nSubject: x\r\n\r\nhi") is None
    raw = b"From: A <a@b.c>\r\nSubject: x\r\nContent-Type: application/pdf\r\n\r\nzzz"
    assert mailnews.parse_message(1, raw) is None  # nothing usable


def test_parse_message_bad_date_and_naive_date() -> None:
    body = b'Content-Type: text/html\r\n\r\n<a href="https://n.example/a">Headline here</a>'
    bad = mailnews.parse_message(1, b"From: A <A@B.c>\r\nDate: garbage\r\n" + body)
    assert bad is not None and bad.date is None and bad.sender_email == "a@b.c"
    naive = mailnews.parse_message(
        2, b"From: A <a@b.c>\r\nDate: Tue, 05 May 2026 10:00:00 -0000\r\n" + body
    )
    assert naive is not None and naive.date is not None and naive.date.tzinfo is not None


def test_body_html_skips_attachments_and_broken_parts() -> None:
    raw = (
        b"From: A <a@b.c>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/html\r\nContent-Disposition: attachment\r\n\r\n<p>att</p>\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nsee https://n.example/a and https://n.example/a\r\n"
        b"--B--\r\n"
    )
    msg = mailnews.parse_message(3, raw)
    assert msg is not None
    assert [c.url for c in msg.candidates] == ["https://n.example/a"]
    assert "att" not in msg.html


def test_imap_connect_selects_ssl_or_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []
    monkeypatch.setattr(
        mailnews, "_TimeoutIMAP4SSL", lambda h, p: made.append(f"ssl:{h}:{p}") or object()
    )
    monkeypatch.setattr(
        mailnews, "_TimeoutIMAP4", lambda h, p: made.append(f"plain:{h}:{p}") or object()
    )
    mailnews._imap_connect("h", 993, True)
    mailnews._imap_connect("h", 143, False)
    assert made == ["ssl:h:993", "plain:h:143"]


def test_uid_search_new_edge_cases() -> None:
    assert mailnews._uid_search_new(FakeConn(search_typ="NO"), 0, 10) == []  # type: ignore[arg-type]
    assert mailnews._uid_search_new(FakeConn(uids=b""), 0, 10) == []  # type: ignore[arg-type]
    assert mailnews._uid_search_new(FakeConn(uids=None), 0, 10) == []  # type: ignore[arg-type]
    assert mailnews._uid_search_new(FakeConn(uids=b"1 5 3 9"), 3, 1) == [5]  # type: ignore[arg-type]


def test_search_new_uids_tolerates_logout_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeConn(logout_fails=True))
    assert mailnews._search_new_uids(_account(last_uid=1)) == [2, 3]


def test_fetch_raw_by_uid_skips_bad_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn(
        logout_fails=True,
        fetch={1: ("OK", [(b"1 (BODY[])", b"raw-one")]), 2: ("NO", []), 3: ("OK", [b")"])},
    )
    _use(monkeypatch, conn)
    assert mailnews._fetch_raw_by_uid(_account(), [1, 2, 3]) == [(1, b"raw-one")]
    assert mailnews._fetch_raw_by_uid(_account(), []) == []


async def test_probe_reports_missing_folder_and_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, FakeConn(select_typ="NO", logout_fails=True))
    res = await mailnews.test_account(_account(folder="Nope"))
    assert res["ok"] is False and "folder not found: Nope" in res["errors"][0]
    _use(monkeypatch, FakeConn())
    assert await mailnews.test_account(_account()) == {"ok": True, "errors": [], "folder": "INBOX"}


def test_extract_batches_splits_when_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    cands = [
        mailnews.LinkCandidate(url=f"https://a.example/{i}", anchor=f"A{i}", context="c" * 50)
        for i in range(4)
    ]
    msg = mailnews.NewsletterMessage(
        uid=1,
        sender_email="a@b.c",
        sender_name="",
        subject="s",
        date=None,
        html="",
        candidates=cands,
    )
    monkeypatch.setattr(mailnews.llm_budget, "input_budget_chars", lambda _m: 1)
    batches = mailnews._extract_batches(msg, cands)
    assert [len(b) for b in batches] == [1, 1, 1, 1]  # halved until single-link batches


async def test_get_or_create_mail_feed_fills_missing_title(
    db_session: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session() as s:
        feed, created = await mailnews.get_or_create_mail_feed(s, "a@b.c", "")
        assert created
        feed.title = ""
        again, created = await mailnews.get_or_create_mail_feed(s, "a@b.c", "Alice")
        assert not created and again.id == feed.id and again.title == "Alice"


async def test_fulltext_batch_seam_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, list[int]]] = []

    async def fake(fid: int, ids: list[int]) -> list[int]:
        seen.append((fid, ids))
        return ids

    monkeypatch.setattr(mailnews, "fetch_full_text_batch", fake)
    assert await mailnews._fulltext_batch(7, [1, 2]) == [1, 2]
    assert seen == [(7, [1, 2])]


async def test_process_message_without_candidates_creates_nothing(
    db_session: async_sessionmaker[AsyncSession],
) -> None:
    msg = mailnews.NewsletterMessage(
        uid=1, sender_email="a@b.c", sender_name="", subject="s", date=None, html="", candidates=[]
    )
    async with db_session() as s:
        assert await mailnews._process_message(s, msg) == 0


async def test_poll_account_times_out_and_releases_lock(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(_a: MailAccount) -> list[Any]:
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(mailnews, "fetch_new_messages", slow)
    monkeypatch.setattr(mailnews, "POLL_LOCK_TIMEOUT_S", 0.05)
    async with db_session() as s:
        account = _account()
        s.add(account)
        await s.commit()
        result = await mailnews.poll_account(s, account)
        assert result == {"messages": 0, "new_articles": 0, "skipped_old": 0}
        await s.refresh(account)
        assert "timed out" in (account.last_error or "")
        assert mailnews.try_begin_poll(account.id)  # lock released


async def test_poll_all_accounts_skips_disabled_and_isolates(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session() as s:
        on = _account(username="on")
        off = _account(username="off", is_enabled=False)
        s.add_all([on, off])
        await s.commit()

    polled: list[str] = []

    async def fake_poll(_s: AsyncSession, account: MailAccount) -> dict[str, int]:
        polled.append(account.username)
        return {}

    monkeypatch.setattr(mailnews, "poll_account", fake_poll)
    monkeypatch.setattr(mailnews, "get_session", _session_from(db_session))
    await mailnews.poll_all_accounts()
    assert polled == ["on"]


def _session_from(maker: async_sessionmaker[AsyncSession]) -> Any:
    async def gen() -> Any:
        async with maker() as s:
            yield s

    return gen


async def test_process_messages_isolates_one_bad_message(
    db_session: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = b'From: A <a@b.c>\r\nContent-Type: text/html\r\n\r\n<a href="https://n.example/a">Headline here</a>'
    calls: list[int] = []

    async def fake_process(_s: AsyncSession, msg: Any, user_id: int | None = None) -> int:
        calls.append(msg.uid)
        if msg.uid == 1:
            raise RuntimeError("boom")
        return 1

    monkeypatch.setattr(mailnews, "_process_message", fake_process)
    async with db_session() as s:
        account = _account()
        s.add(account)
        await s.commit()
        res = await mailnews.process_messages(
            s, account, [mailnews.FetchedMail(1, good), mailnews.FetchedMail(2, good)]
        )
        assert calls == [1, 2]
        assert res["new_articles"] == 1
