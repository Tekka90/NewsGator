"""Reader sync helpers: poll lock, cutoff, paging order, checkpoint, sweep, outbound push."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.config import settings
from app.models import Article, Feed, ReaderAccount, Story, User
from app.services.readers import sync
from app.services.readers.greader import GReaderClient, GReaderItem


def _item(i: int, crawled: int | None) -> GReaderItem:
    return GReaderItem(
        id=f"id{i}",
        title=f"t{i}",
        url=f"https://x/{i}",
        raw_content="",
        published_at=None,
        origin_feed_title=None,
        is_read=False,
        crawled_at=datetime.fromtimestamp(crawled, tz=UTC) if crawled is not None else None,
    )


class FakeClient:
    """fetch_stream replacement yielding scripted (items, continuation) pages."""

    def __init__(self, pages: list[tuple[list[GReaderItem], str | None]]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    async def fetch_stream(self, **kw: Any) -> tuple[list[GReaderItem], str | None]:
        self.calls.append(kw)
        return self.pages.pop(0) if self.pages else ([], None)


@pytest.fixture(autouse=True)
def _clear() -> None:
    sync._polling_accounts.clear()


def test_poll_lock_blocks_until_released_or_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sync.try_begin_poll(1) is True
    assert sync.try_begin_poll(1) is False
    assert sync.try_begin_poll(2) is True
    sync.end_poll(1)
    assert sync.try_begin_poll(1) is True
    sync.end_poll(99)  # unknown id is fine
    monkeypatch.setattr(sync, "POLL_LOCK_TIMEOUT_S", -1.0)
    assert sync.try_begin_poll(1) is True  # stale lock self-heals


async def test_poll_returns_zero_when_already_running() -> None:
    acc = ReaderAccount(id=7, user_id=1, api_base_url="https://x")
    assert sync.try_begin_poll(7)
    assert await sync.poll_reader_account(None, acc) == {  # type: ignore[arg-type]
        "new_articles": 0,
        "read_synced": 0,
    }
    assert 7 in sync._polling_accounts  # not released by the skipped call


async def test_poll_releases_lock_when_inner_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: object) -> dict[str, int]:
        raise RuntimeError("x")

    monkeypatch.setattr(sync, "_poll_reader_account_inner", boom)
    acc = ReaderAccount(id=8, user_id=1, api_base_url="https://x")
    with pytest.raises(RuntimeError):
        await sync.poll_reader_account(None, acc)  # type: ignore[arg-type]
    assert 8 not in sync._polling_accounts


def test_reader_cutoff_follows_account_then_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "feed_backfill_days", 3)
    acc = ReaderAccount(user_id=1, api_base_url="x", backfill_days=None)
    cutoff = sync._reader_cutoff(acc)
    assert cutoff is not None
    assert abs((datetime.now(UTC) - cutoff) - timedelta(days=3)) < timedelta(seconds=5)
    acc.backfill_days = 0
    assert sync._reader_cutoff(acc) is None  # 0 = everything
    monkeypatch.setattr(settings, "feed_backfill_days", 0)
    acc.backfill_days = None
    assert sync._reader_cutoff(acc) is None
    acc.backfill_days = 10
    assert sync._reader_cutoff(acc) is not None


async def test_checkpoint_uses_oldest_seen_and_follows_continuations() -> None:
    client = FakeClient(
        [([_item(1, 500), _item(2, 300)], "c1"), ([_item(3, 400), _item(4, None)], "c1")]
    )
    acc = ReaderAccount(user_id=1, api_base_url="x", initial_import_count=10, sync_cursor="old")
    await sync.establish_reader_checkpoint(client, acc)  # type: ignore[arg-type]
    assert acc.sync_checkpoint == 300 and acc.sync_cursor is None
    # second call repeats the same continuation token → stops there
    assert [c["continuation"] for c in client.calls] == [None, "c1"]
    assert all(c["exclude_read"] for c in client.calls)


async def test_checkpoint_limits_page_size_to_remaining_wanted() -> None:
    client = FakeClient([([_item(1, 100), _item(2, 90)], "c1")])
    acc = ReaderAccount(user_id=1, api_base_url="x", initial_import_count=2)
    await sync.establish_reader_checkpoint(client, acc)  # type: ignore[arg-type]
    assert len(client.calls) == 1 and client.calls[0]["limit"] == 2
    assert acc.sync_checkpoint == 90


async def test_checkpoint_empty_stream_starts_now_minus_skew() -> None:
    before = int(datetime.now(UTC).timestamp())
    acc = ReaderAccount(user_id=1, api_base_url="x", initial_import_count=5)
    await sync.establish_reader_checkpoint(FakeClient([]), acc)  # type: ignore[arg-type]
    assert (
        before - sync.CLOCK_SKEW_MARGIN_S
        <= (acc.sync_checkpoint or 0)
        <= (before + 5 - sync.CLOCK_SKEW_MARGIN_S)
    )


async def _collect(client: FakeClient, checkpoint: int) -> list[list[str]]:
    return [[i.id for i in page] async for page in sync._unread_pages(client, checkpoint)]  # type: ignore[arg-type]


async def test_unread_pages_replays_newest_first_service_oldest_first() -> None:
    client = FakeClient(
        [
            ([_item(1, 300), _item(2, 200)], "n1"),  # ignores r=o: descending
            ([_item(3, 100)], None),
        ]
    )
    pages = await _collect(client, 50)
    assert pages == [["id3", "id2", "id1"]]
    assert client.calls[1]["continuation"] == "n1"


async def test_unread_pages_advances_checkpoint_and_stops_when_caught_up() -> None:
    client = FakeClient([([_item(1, 100), _item(2, 200)], None), ([_item(2, 200)], None)])
    pages = await _collect(client, 50)
    assert pages == [["id1", "id2"], ["id2"]]
    assert [c["oldest_ts"] for c in client.calls] == [50, 200]
    assert all(c["oldest_first"] for c in client.calls)


async def test_unread_pages_full_page_with_same_second_steps_past_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "READER_PAGE_SIZE", 2)
    client = FakeClient([([_item(1, 100), _item(2, 100)], None), ([_item(3, 101)], None)])
    pages = await _collect(client, 100)
    assert [c["oldest_ts"] for c in client.calls] == [100, 101]
    assert pages == [["id1", "id2"], ["id3"]]


async def test_unread_pages_without_crawl_times_yields_once() -> None:
    client = FakeClient([([_item(1, None)], None), ([_item(2, None)], None)])
    assert await _collect(client, 0) == [["id1"]]
    assert len(client.calls) == 1


async def test_poll_all_skips_disabled_and_survives_failures(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session() as s:
        user = User(username="u", password_hash="x", is_admin=True)
        s.add(user)
        await s.flush()
        for title, enabled in [("a", True), ("b", True), ("off", False)]:
            s.add(
                ReaderAccount(
                    user_id=user.id, api_base_url="https://x", title=title, is_enabled=enabled
                )
            )
        await s.commit()

    polled: list[str] = []

    async def fake_poll(_s: object, account: ReaderAccount) -> dict[str, int]:
        polled.append(account.title)
        if account.title == "a":
            raise RuntimeError("down")
        return {}

    monkeypatch.setattr(sync, "poll_reader_account", fake_poll)
    await sync.poll_all_reader_accounts()
    assert polled == ["a", "b"]


async def test_outbound_push_only_enabled_owned_accounts_and_saves_rotated_token(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session() as s:
        user = User(username="u", password_hash="x", is_admin=True)
        other = User(username="o", password_hash="x")
        s.add_all([user, other])
        feed = Feed(url="reader://1", title="R", kind="reader_api")
        feed2 = Feed(url="reader://2", title="R2", kind="reader_api")
        s.add_all([feed, feed2])
        await s.flush()
        story = Story(title="S", summary="x", category="c")
        s.add(story)
        await s.flush()
        s.add_all(
            [
                Article(
                    feed_id=feed.id, guid="g1", url="https://x/1", title="1", story_id=story.id
                ),
                Article(feed_id=feed.id, guid="", url="https://x/2", title="2", story_id=story.id),
                Article(
                    feed_id=feed2.id, guid="g3", url="https://x/3", title="3", story_id=story.id
                ),
            ]
        )
        s.add(
            ReaderAccount(
                user_id=user.id, api_base_url="https://x", virtual_feed_id=feed.id, auth_token="old"
            )
        )
        # feed2 belongs to another user's account → never pushed for `user`
        s.add(ReaderAccount(user_id=other.id, api_base_url="https://x", virtual_feed_id=feed2.id))
        await s.commit()
        uid, sid = user.id, story.id

    pushed: list[tuple[list[str], bool]] = []

    async def fake_set(self: GReaderClient, item_ids: list[str], is_read: bool) -> None:
        pushed.append((item_ids, is_read))
        self.auth_token = "rotated"

    monkeypatch.setattr(GReaderClient, "set_read_state", fake_set)
    async with db_session() as s:
        assert await sync.sync_story_read_state_outbound(s, uid, sid, is_read=True) == 1
        assert await sync.sync_story_read_state_outbound(s, uid, 99999, is_read=True) == 0
    assert pushed == [(["g1"], True)]
    async with db_session() as s:
        from sqlalchemy import select

        acc = (await s.scalars(select(ReaderAccount).where(ReaderAccount.user_id == uid))).one()
        assert acc.auth_token == "rotated"

    async def failing(self: GReaderClient, item_ids: list[str], is_read: bool) -> None:
        raise RuntimeError("upstream")

    monkeypatch.setattr(GReaderClient, "set_read_state", failing)
    async with db_session() as s:
        assert await sync.sync_story_read_state_outbound(s, uid, sid, is_read=False) == 0


# --- DB-backed pieces ---------------------------------------------------------


async def _user_account(s: Any, **kw: Any) -> tuple[User, ReaderAccount]:
    user = User(username="u", password_hash="x", is_admin=True)
    s.add(user)
    await s.flush()
    acc = ReaderAccount(user_id=user.id, api_base_url="https://x", username="bob", **kw)
    s.add(acc)
    await s.flush()
    return user, acc


async def test_ensure_virtual_feed_creates_once_and_subscribes_owner(db_session) -> None:
    from sqlalchemy import func, select

    from app.models import UserFeed

    async with db_session() as s:
        user, acc = await _user_account(s)
        feed = await sync.ensure_virtual_feed(s, acc)
        assert feed.kind == "reader_api" and feed.url == f"reader:greader:{acc.id}"
        assert feed.title == "Greader (bob)"
        assert acc.virtual_feed_id == feed.id
        again = await sync.ensure_virtual_feed(s, acc)
        assert again.id == feed.id
        # lost link (virtual_feed_id cleared) → found again through the pseudo url
        acc.virtual_feed_id = None
        acc.title = "  Named "
        assert (await sync.ensure_virtual_feed(s, acc)).id == feed.id
        await s.flush()
        count = await s.scalar(
            select(func.count()).select_from(UserFeed).where(UserFeed.user_id == user.id)
        )
        assert count == 1


async def test_ingest_inbound_read_state_single_vs_multi_source(db_session) -> None:
    from sqlalchemy import select

    from app.models import StoryState

    async with db_session() as s:
        user, acc = await _user_account(s)
        feed = await sync.ensure_virtual_feed(s, acc)
        solo = Story(title="Solo", summary="x", category="c", version=3)
        multi = Story(title="Multi", summary="x", category="c", version=3)
        s.add_all([solo, multi])
        await s.flush()
        s.add_all(
            [
                Article(feed_id=feed.id, guid="a", url="https://x/a", title="a", story_id=solo.id),
                Article(feed_id=feed.id, guid="b", url="https://x/b", title="b", story_id=multi.id),
                Article(feed_id=feed.id, guid="c", url="https://x/c", title="c", story_id=multi.id),
            ]
        )
        await s.flush()
        items = [_item(0, 1), _item(1, 2)]
        items[0].id, items[0].is_read = "a", True
        items[1].id, items[1].is_read = "b", True
        new, synced, pending, handoff = await sync._ingest_items(s, acc, feed, items)
        assert (new, synced, pending, handoff) == (0, 2, [], [])
        states = {
            st.story_id: st
            for st in (await s.scalars(select(StoryState).where(StoryState.user_id == user.id)))
        }
        assert states[solo.id].is_read and states[solo.id].read_at_version == 3
        # other sources still unread → shows as "updated", not fully read
        assert states[multi.id].is_read and states[multi.id].read_at_version == 2
        # idempotent: second pass changes nothing
        _, synced_again, _, _ = await sync._ingest_items(s, acc, feed, items)
        assert synced_again == 0


async def test_ingest_new_items_route_by_fulltext_flag(db_session) -> None:
    async with db_session() as s:
        _user, acc = await _user_account(s)
        feed = await sync.ensure_virtual_feed(s, acc)
        feed.fetch_fulltext = True
        new, _, pending, handoff = await sync._ingest_items(s, acc, feed, [_item(1, 1)])
        assert new == 1 and len(pending) == 1 and handoff == []
        feed.fetch_fulltext = False
        new, _, pending, handoff = await sync._ingest_items(s, acc, feed, [_item(2, 2)])
        assert new == 1 and pending == [] and len(handoff) == 1
        art = await s.get(Article, handoff[0])
        assert art is not None and art.processing_state == "fulltext"


async def test_process_page_enqueues_handoff_and_fetched_fulltext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueued: list[int] = []

    async def fake_fetch(feed_id: int, ids: list[int]) -> list[int]:
        assert (feed_id, ids) == (5, [3, 4])
        return [4]

    monkeypatch.setattr(sync, "fetch_full_text_batch", fake_fetch)
    monkeypatch.setattr(sync, "enqueue_article", enqueued.append)
    await sync._process_page(5, [3, 4], [1, 2])
    assert enqueued == [1, 2, 4]
    enqueued.clear()
    await sync._process_page(5, [], [9])
    assert enqueued == [9]


async def test_poll_failure_records_error_and_reraises(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from app.models import ActivityEvent

    async with db_session() as s:
        _user, acc = await _user_account(s, sync_checkpoint=10)
        await s.commit()
        aid = acc.id

    async def boom(self: GReaderClient, **_kw: Any) -> tuple[list[GReaderItem], str | None]:
        raise RuntimeError("stream down")

    monkeypatch.setattr(GReaderClient, "fetch_stream", boom)
    async with db_session() as s:
        acc = await s.get(ReaderAccount, aid)
        assert acc is not None
        with pytest.raises(RuntimeError, match="stream down"):
            await sync.poll_reader_account(s, acc)
        assert aid not in sync._polling_accounts
    async with db_session() as s:
        acc = await s.get(ReaderAccount, aid)
        assert acc is not None and acc.last_error == "stream down"
        actions = [e.action for e in await s.scalars(select(ActivityEvent))]
        assert "reader_poll_failed" in actions and "reader_poll_done" not in actions


async def test_poll_imports_pages_advances_checkpoint_and_filters_old(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    monkeypatch.setattr(settings, "feed_backfill_days", 0)
    handed: list[int] = []
    monkeypatch.setattr(sync, "enqueue_article", handed.append)

    async def no_fulltext(feed_id: int, ids: list[int]) -> list[int]:
        return ids

    monkeypatch.setattr(sync, "fetch_full_text_batch", no_fulltext)
    async with db_session() as s:
        _user, acc = await _user_account(s, sync_checkpoint=None, backfill_days=2)
        await s.commit()
        aid = acc.id

    base_ts = int(datetime.now(UTC).timestamp())
    old = _item(1, base_ts - 100)
    old.published_at = datetime.now(UTC) - timedelta(days=30)
    recent = _item(2, base_ts - 50)
    recent.published_at = datetime.now(UTC) - timedelta(hours=1)
    now_ts = int(datetime.now(UTC).timestamp())

    async def fetch(self: GReaderClient, **kw: Any) -> tuple[list[GReaderItem], str | None]:
        if kw.get("oldest_first"):
            return ([old, recent], None) if kw["oldest_ts"] < base_ts - 50 else ([], None)
        return [], None  # establish_reader_checkpoint: empty stream

    monkeypatch.setattr(GReaderClient, "fetch_stream", fetch)
    async with db_session() as s:
        acc = await s.get(ReaderAccount, aid)
        assert acc is not None
        res = await sync.poll_reader_account(s, acc)
    assert res == {"new_articles": 1, "read_synced": 0}
    async with db_session() as s:
        acc = await s.get(ReaderAccount, aid)
        assert acc is not None and acc.last_error is None and acc.last_checked_at is not None
        # checkpoint was raised to the 2-day cutoff (> establish's skew fallback)
        assert acc.sync_checkpoint is not None and acc.sync_checkpoint > now_ts - 3 * 86400
        titles = [a.title for a in await s.scalars(select(Article))]
        assert titles == ["t2"]
    assert len(handed) == 1
