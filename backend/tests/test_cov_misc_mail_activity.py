"""Mail account API (poll/patch/delete flows) and activity SSE generator."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.api import activity as activity_api
from app.models import ActivityEvent, MailAccount, User
from app.services import activity, mailnews

ACCOUNT = {
    "host": " imap.example.com ",
    "username": "me@example.com",
    "password": "pw",
    "folder": "News",
}


@pytest.fixture(autouse=True)
def _clear_locks() -> None:
    mailnews._polling_accounts.clear()


async def _make(client: AsyncClient) -> int:
    await setup_admin(client)
    r = await client.post("/api/mail-accounts", json=ACCOUNT)
    assert r.status_code == 201
    assert r.json()["host"] == "imap.example.com"  # stripped
    return int(r.json()["id"])


async def _actions(db_session) -> list[str]:
    async with db_session() as s:
        return [
            e.action for e in (await s.scalars(select(ActivityEvent).order_by(ActivityEvent.id)))
        ]


async def _account(db_session, aid: int) -> MailAccount:
    async with db_session() as s:
        acc = await s.get(MailAccount, aid)
        assert acc is not None
        return acc


async def test_patch_ignores_null_and_strips_and_can_disable(
    client: AsyncClient, db_session
) -> None:
    aid = await _make(client)
    r = await client.patch(
        f"/api/mail-accounts/{aid}",
        json={"username": "  new@example.com ", "password": None, "is_enabled": False, "port": 143},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "new@example.com" and body["port"] == 143
    assert body["is_enabled"] is False
    acc = await _account(db_session, aid)
    assert acc.password  # untouched by a null password patch

    r = await client.post(f"/api/mail-accounts/{aid}/poll")
    assert r.status_code == 400
    assert (await client.patch("/api/mail-accounts/9999", json={})).status_code == 404


async def test_delete_removes_and_logs(client: AsyncClient, db_session) -> None:
    aid = await _make(client)
    assert (await client.delete(f"/api/mail-accounts/{aid}")).status_code == 204
    assert (await client.get("/api/mail-accounts")).json() == []
    actions = await _actions(db_session)
    assert actions == ["mail_account_created", "mail_account_deleted"]


async def test_poll_conflict_when_already_running(client: AsyncClient) -> None:
    aid = await _make(client)
    assert mailnews.try_begin_poll(aid)
    r = await client.post(f"/api/mail-accounts/{aid}/poll")
    assert r.status_code == 409


async def test_poll_search_failure_is_502_and_releases_lock(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = await _make(client)

    async def boom(_acc: object) -> list[int]:
        raise OSError("imap down")

    monkeypatch.setattr(mailnews, "search_new_uids", boom)
    r = await client.post(f"/api/mail-accounts/{aid}/poll")
    assert r.status_code == 502 and "imap down" in r.json()["detail"]
    assert aid not in mailnews._polling_accounts
    acc = await _account(db_session, aid)
    assert acc.last_error == "OSError: imap down"
    assert "mail_poll_failed" in await _actions(db_session)


async def test_poll_without_new_mail_reports_zero(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = await _make(client)

    async def none(_acc: object) -> list[int]:
        return []

    monkeypatch.setattr(mailnews, "search_new_uids", none)
    r = await client.post(f"/api/mail-accounts/{aid}/poll")
    assert r.status_code == 202
    assert r.json() == {"found": 0, "processing": False}
    acc = await _account(db_session, aid)
    assert acc.last_checked_at is not None and acc.last_error is None
    assert aid not in mailnews._polling_accounts


async def test_poll_processes_found_messages(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = await _make(client)
    seen: dict[str, object] = {}

    async def uids(_acc: object) -> list[int]:
        return [4, 7]

    async def fetch(_acc: object, wanted: list[int]) -> list[str]:
        seen["uids"] = wanted
        return ["m1", "m2"]

    async def process(_s: object, _acc: object, msgs: list[str]) -> None:
        seen["msgs"] = msgs

    monkeypatch.setattr(mailnews, "search_new_uids", uids)
    monkeypatch.setattr(mailnews, "fetch_messages_by_uid", fetch)
    monkeypatch.setattr(mailnews, "process_messages", process)
    r = await client.post(f"/api/mail-accounts/{aid}/poll")
    assert r.json() == {"found": 2, "processing": False}
    assert seen == {"uids": [4, 7], "msgs": ["m1", "m2"]}
    assert aid not in mailnews._polling_accounts


async def test_poll_background_fetch_error_recorded(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = await _make(client)

    async def uids(_acc: object) -> list[int]:
        return [1]

    async def fetch(_acc: object, _w: list[int]) -> list[str]:
        raise RuntimeError("fetch broke")

    monkeypatch.setattr(mailnews, "search_new_uids", uids)
    monkeypatch.setattr(mailnews, "fetch_messages_by_uid", fetch)
    await client.post(f"/api/mail-accounts/{aid}/poll")
    acc = await _account(db_session, aid)
    assert acc.last_error == "RuntimeError: fetch broke"
    assert "mail_fetch_error" in await _actions(db_session)


async def test_poll_background_process_error_rolls_back_and_logs(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = await _make(client)

    async def uids(_acc: object) -> list[int]:
        return [1]

    async def fetch(_acc: object, _w: list[int]) -> list[str]:
        return ["m"]

    async def process(*_a: object) -> None:
        raise ValueError("bad body")

    monkeypatch.setattr(mailnews, "search_new_uids", uids)
    monkeypatch.setattr(mailnews, "fetch_messages_by_uid", fetch)
    monkeypatch.setattr(mailnews, "process_messages", process)
    await client.post(f"/api/mail-accounts/{aid}/poll")
    assert "mail_process_error" in await _actions(db_session)
    assert aid not in mailnews._polling_accounts


async def test_background_timeout_sets_error_and_releases_lock(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import mail as mail_api

    aid = await _make(client)

    async def slow(_acc: object, _w: list[int]) -> list[str]:
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(mailnews, "fetch_messages_by_uid", slow)
    monkeypatch.setattr(mailnews, "POLL_LOCK_TIMEOUT_S", 0.05)
    assert mailnews.try_begin_poll(aid)
    await mail_api._process_in_background(aid, [1])
    acc = await _account(db_session, aid)
    assert acc.last_error and "timed out" in acc.last_error
    assert aid not in mailnews._polling_accounts


async def test_background_missing_account_is_noop(db_session) -> None:
    from app.api import mail as mail_api

    await mail_api._process_in_background(424242, [1])


async def test_other_user_cannot_poll(client: AsyncClient) -> None:
    aid = await _make(client)
    await client.post("/api/users", json={"username": "reader", "password": "readerpass1"})
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "reader", "password": "readerpass1"})
    assert (await client.post(f"/api/mail-accounts/{aid}/poll")).status_code == 404
    assert (await client.post(f"/api/mail-accounts/{aid}/test")).status_code == 404


# --- activity -----------------------------------------------------------------


async def test_recent_filters_component_and_orders_oldest_first(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    async with db_session() as s:
        await activity.emit(s, "feed", "one", {"n": 1})
        await activity.emit(s, "mail", "two", {"n": 2})
        await activity.emit(s, "feed", "three", {"n": 3})
        await s.commit()
    r = await client.get("/api/activity/recent", params={"component": "feed"})
    events = r.json()["events"]
    assert [e["action"] for e in events] == ["one", "three"]
    assert events[0]["detail"] == {"n": 1}
    r = await client.get("/api/activity/recent", params={"limit": 1})
    assert [e["action"] for e in r.json()["events"]] == ["three"]


async def test_llm_trace_snapshot_caps_limit(client: AsyncClient, monkeypatch) -> None:
    await setup_admin(client)
    seen: list[int] = []
    monkeypatch.setattr(activity_api.llmtrace, "recent", lambda n: seen.append(n) or [])
    r = await client.get("/api/activity/llm", params={"limit": 5000})
    assert r.json()["interactions"] == []
    assert seen == [100]


async def test_stream_hello_event_ping_and_unsubscribe(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    async with db_session() as s:
        user = (await s.scalars(select(User))).one()
    resp = await activity_api.stream(user)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-accel-buffering"] == "no"
    gen = resp.body_iterator

    hello = await anext(gen)
    assert hello.startswith("event: hello")

    async with db_session() as s:
        await activity.emit(s, "feed", "live", {"x": 1})
        await s.commit()
    msg = json.loads((await anext(gen)).removeprefix("data: "))
    assert msg["action"] == "live" and "ts" in msg

    async def timeout(coro: object, **_k: object) -> None:
        coro.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr(activity_api, "asyncio", SimpleNamespace(wait_for=timeout))
    ping = json.loads((await anext(gen)).removeprefix("data: "))
    assert ping == {"action": "ping"}

    before = len(activity._subscribers)
    await gen.aclose()
    assert len(activity._subscribers) == before - 1


async def test_pipeline_snapshot_splits_in_flight_and_finished(
    client: AsyncClient, db_session
) -> None:
    from app.models import Article, Feed

    await setup_admin(client)
    async with db_session() as s:
        feed = Feed(url="https://f.example.com/rss", title="Feed A")
        s.add(feed)
        await s.flush()
        for i, state in enumerate(["fetched", "summarized", "clustered"]):
            s.add(
                Article(
                    feed_id=feed.id,
                    guid=f"g{i}",
                    url=f"https://x/{i}",
                    title=f"T{i}",
                    processing_state=state,
                )
            )
        await s.commit()
    body = (await client.get("/api/activity/pipeline")).json()
    assert body["states"][0] == "fetched" and body["states"][-1] == "clustered"
    assert body["in_flight"] == 2 and body["truncated"] is False
    assert [r["processing_state"] for r in body["rows"]] == ["summarized", "fetched", "clustered"]
    assert {r["feed_title"] for r in body["rows"]} == {"Feed A"}


async def test_poll_outside_test_env_processes_in_background(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import mail as mail_api
    from app.core.config import settings

    aid = await _make(client)
    done = asyncio.Event()

    async def uids(_acc: object) -> list[int]:
        return [1, 2, 3]

    async def fake_bg(account_id: int, wanted: list[int]) -> None:
        assert (account_id, wanted) == (aid, [1, 2, 3])
        done.set()

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(mailnews, "search_new_uids", uids)
    monkeypatch.setattr(mail_api, "_process_in_background", fake_bg)
    r = await client.post(f"/api/mail-accounts/{aid}/poll")
    assert r.json() == {"found": 3, "processing": True}
    await asyncio.wait_for(done.wait(), 2)
    await asyncio.sleep(0)
    assert not mail_api._background_mail
