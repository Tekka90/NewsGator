"""API edge cases: story feed, chat gating/history, usage periods, deps, ops, db setup."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.api import chat as chat_api
from app.api import feed as feed_api
from app.api import ops
from app.core import db
from app.core.config import settings
from app.core.security import make_session_token
from app.models import (
    Article,
    ChatMessage,
    Feed,
    LLMUsage,
    Story,
    StoryTranslation,
    User,
    UserFeed,
)
from app.services import chat as chat_service


async def _admin_id(db_session) -> int:
    async with db_session() as s:
        return int((await s.scalars(select(User.id))).first() or 0)


# --- story feed ---------------------------------------------------------------


def test_rfc822_treats_naive_as_utc() -> None:
    naive = feed_api._rfc822(datetime(2026, 1, 2, 3, 4, 5))
    aware = feed_api._rfc822(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    assert naive == aware and naive.endswith("+0000")


def test_summary_to_html_escapes_and_splits_paragraphs() -> None:
    assert (
        feed_api._summary_to_html("a <b>\n\n\n\n  c & d ") == "<p>a &lt;b&gt;</p><p>c &amp; d</p>"
    )


def test_render_rss_omits_link_and_image_when_absent() -> None:
    story = Story(id=5, title="T", summary="S", category="cat")
    xml = feed_api.render_rss(
        [feed_api.FeedItem(story, None, None, datetime(2026, 1, 1))],
        channel_title="C",
        self_url="https://s/feed.xml",
    )
    item = fromstring(xml).find("channel").find("item")
    assert item.find("link") is None
    assert not [c for c in item if c.tag.endswith("content")]
    assert item.findtext("pubDate").startswith("Thu, 01 Jan 2026")


async def test_feed_empty_when_user_has_no_feeds_or_no_stories(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    uid = await _admin_id(db_session)
    r = await client.get("/api/feed.xml")
    assert fromstring(r.text).find("channel").findall("item") == []
    assert r.headers["content-type"].startswith("application/rss+xml")

    async with db_session() as s:
        feed = Feed(url="https://f.example/rss", title="F")
        s.add(feed)
        await s.flush()
        for uf in (await s.scalars(select(UserFeed).where(UserFeed.user_id == uid))).all():
            await s.delete(uf)
        s.add(UserFeed(user_id=uid, feed_id=feed.id))
        s.add(Article(feed_id=feed.id, guid="g", url="https://x/1", title="unclustered"))
        await s.commit()
    r = await client.get("/api/feed.xml")
    assert fromstring(r.text).find("channel").findall("item") == []


async def test_feed_serves_cached_translation_only_for_current_version(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    uid = await _admin_id(db_session)
    async with db_session() as s:
        user = await s.get(User, uid)
        user.summary_language = "fr"
        feed = Feed(url="https://f.example/rss", title="F")
        s.add(feed)
        await s.flush()
        s.add(UserFeed(user_id=uid, feed_id=feed.id))
        fresh = Story(title="Fresh EN", summary="en one", category="c", language="en", version=2)
        stale = Story(title="Stale EN", summary="en two", category="c", language="en", version=3)
        native = Story(title="Déjà FR", summary="fr natif", category="c", language="fr")
        s.add_all([fresh, stale, native])
        await s.flush()
        for i, st in enumerate([fresh, stale, native]):
            s.add(
                Article(
                    feed_id=feed.id, guid=f"g{i}", url=f"https://x/{i}", title="a", story_id=st.id
                )
            )
        s.add(
            StoryTranslation(
                story_id=fresh.id, language="fr", version=2, title="Frais FR", summary="fr un"
            )
        )
        s.add(
            StoryTranslation(
                story_id=stale.id, language="fr", version=1, title="Vieux FR", summary="fr deux"
            )
        )
        s.add(
            StoryTranslation(
                story_id=native.id, language="fr", version=1, title="IGNORED", summary="IGNORED"
            )
        )
        await s.commit()
    r = await client.get("/api/feed.xml")
    titles = {i.findtext("title") for i in fromstring(r.text).find("channel").findall("item")}
    assert titles == {"Frais FR", "Stale EN", "Déjà FR"}


# --- chat ---------------------------------------------------------------------


async def test_chat_endpoints_404_when_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "chat_enabled", False)
    assert (await client.post("/api/chat", json={"question": "hi"})).status_code == 404
    assert (await client.get("/api/chat/history")).status_code == 404
    assert (await client.delete("/api/chat/history")).status_code == 404


async def test_chat_ask_maps_chat_error_to_502_and_passes_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "chat_enabled", True)
    seen: dict[str, object] = {}

    async def failing(_s: object, question: str, **kw: object) -> dict[str, object]:
        seen.update(question=question, **kw)
        raise chat_service.ChatError("model unreachable")

    monkeypatch.setattr(chat_api.chat, "ask", failing)
    r = await client.post("/api/chat", json={"question": "what happened?"})
    assert r.status_code == 502 and r.json()["detail"] == "model unreachable"
    assert seen["question"] == "what happened?" and seen["username"] == "admin"
    assert (await client.post("/api/chat", json={"question": ""})).status_code == 422


async def test_chat_history_tolerates_corrupt_story_payloads_and_clears(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(settings, "chat_enabled", True)
    uid = await _admin_id(db_session)
    good = [
        {
            "id": 1,
            "title": "T",
            "category": "c",
            "image_url": None,
            "last_updated_at": "2026-01-01T00:00:00Z",
            "source_hosts": ["a.example"],
            "similarity": 0.5,
            "cited": True,
        }
    ]
    t0 = datetime.now(UTC)
    async with db_session() as s:
        s.add_all(
            [
                ChatMessage(user_id=uid, role="user", content="q", created_at=t0),
                ChatMessage(
                    user_id=uid,
                    role="assistant",
                    content="a",
                    latency_ms=12,
                    stories_json=json.dumps(good),
                    created_at=t0 + timedelta(seconds=1),
                ),
                ChatMessage(
                    user_id=uid,
                    role="assistant",
                    content="bad json",
                    stories_json="{nope",
                    created_at=t0 + timedelta(seconds=2),
                ),
                ChatMessage(
                    user_id=uid,
                    role="assistant",
                    content="not list",
                    stories_json='{"a":1}',
                    created_at=t0 + timedelta(seconds=3),
                ),
            ]
        )
        await s.commit()
    hist = (await client.get("/api/chat/history")).json()
    assert [h["content"] for h in hist] == ["q", "a", "bad json", "not list"]
    assert hist[1]["stories"][0]["title"] == "T" and hist[1]["latency_ms"] == 12
    assert hist[2]["stories"] == [] and hist[3]["stories"] == []
    assert (await client.delete("/api/chat/history")).status_code == 204
    assert (await client.get("/api/chat/history")).json() == []


# --- usage --------------------------------------------------------------------


async def test_usage_summary_period_windows_and_throughput(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    now = datetime.now(UTC)
    async with db_session() as s:
        s.add_all(
            [
                LLMUsage(
                    ts=now,
                    kind="summarize",
                    endpoint="chat",
                    model="m",
                    prompt_tokens=10,
                    completion_tokens=200,
                    total_tokens=210,
                    latency_ms=1000,
                ),
                LLMUsage(
                    ts=now,
                    kind="embed",
                    endpoint="embed",
                    model="e",
                    prompt_tokens=50,
                    completion_tokens=0,
                    total_tokens=50,
                    latency_ms=500,
                ),
                LLMUsage(
                    ts=now,
                    kind="chat",
                    endpoint="chat",
                    model="m",
                    prompt_tokens=5,
                    completion_tokens=5,
                    total_tokens=10,
                    latency_ms=0,
                ),
                LLMUsage(
                    ts=now - timedelta(days=400),
                    kind="summarize",
                    endpoint="chat",
                    model="m",
                    prompt_tokens=1,
                    completion_tokens=1,
                    total_tokens=2,
                    latency_ms=10,
                ),
            ]
        )
        await s.commit()
    day = (await client.get("/api/usage/summary", params={"period": "day"})).json()
    assert day["totals"]["calls"] == 3 and day["totals"]["total_tokens"] == 270
    by_kind = {k["kind"]: k for k in day["by_kind"]}
    assert by_kind["summarize"]["tokens_per_s"] == 200.0  # completion tok/s
    assert by_kind["embed"]["tokens_per_s"] == 100.0  # prompt tok/s for embeddings
    assert by_kind["chat"]["tokens_per_s"] is None  # no latency recorded
    assert (await client.get("/api/usage/summary", params={"period": "month"})).json()["totals"][
        "calls"
    ] == 3
    everything = (await client.get("/api/usage/summary")).json()
    assert everything["totals"]["calls"] == 4
    assert (await client.get("/api/usage/summary", params={"period": "year"})).status_code == 422


# --- deps / ops ---------------------------------------------------------------


async def test_session_token_for_deleted_user_is_rejected(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    token = make_session_token(424242)
    client.cookies.clear()
    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["detail"] == "Unknown user"
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.json()["detail"] == "Not authenticated"


def test_commit_prefers_env_and_degrades_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_COMMIT", "abcdef1234567890")
    assert ops._commit() == "abcdef123456"
    monkeypatch.delenv("GIT_COMMIT")

    def no_git(*_a: object, **_k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(ops.subprocess, "run", no_git)
    assert ops._commit() == "unknown"
    monkeypatch.setattr(ops.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="  \n"))
    assert ops._commit() == "unknown"
    monkeypatch.setattr(
        ops.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="1a2b3c4\n")
    )
    assert ops._commit() == "1a2b3c4"


async def test_health_is_public_and_reports_features(client: AsyncClient) -> None:
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok" and body["features"] == ops.FEATURES


# --- db -----------------------------------------------------------------------


def test_unwrap_sqlite_conn_walks_wrappers() -> None:
    raw = sqlite3.connect(":memory:")
    wrapped = SimpleNamespace(driver_connection=SimpleNamespace(_connection=raw))
    assert db._unwrap_sqlite_conn(wrapped) is raw
    assert db._unwrap_sqlite_conn(raw) is raw
    opaque = SimpleNamespace()
    assert db._unwrap_sqlite_conn(opaque) is opaque
    selfref = SimpleNamespace()
    selfref._connection = selfref
    assert db._unwrap_sqlite_conn(selfref) is selfref
    raw.close()


def test_configure_sqlite_registers_normalizer_and_ignores_foreign_connections() -> None:
    db._configure_sqlite_on_connect(object(), None)  # not sqlite → no-op
    conn = sqlite3.connect(":memory:")
    db._configure_sqlite_on_connect(conn, None)
    assert conn.execute("select ng_norm('ÉCOLE')").fetchone()[0] == "ecole"
    assert conn.execute("pragma busy_timeout").fetchone()[0] == 30000
    conn.close()


async def test_new_session_yields_working_session(db_session) -> None:
    async with db.new_session() as s:
        assert (await s.scalar(select(User.id).limit(1))) is None


async def test_usage_daily_and_by_feed_group_and_admin_only(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    now = datetime.now(UTC)
    async with db_session() as s:
        feed = Feed(url="https://f.example/rss", title="Feed F")
        s.add(feed)
        await s.flush()
        s.add_all(
            [
                LLMUsage(
                    ts=now,
                    kind="summarize",
                    model="m",
                    total_tokens=30,
                    latency_ms=1,
                    feed_id=feed.id,
                ),
                LLMUsage(ts=now, kind="chat", model="m", total_tokens=10, latency_ms=1),
                LLMUsage(
                    ts=now, kind="chat", model="m", total_tokens=5, latency_ms=1, feed_id=9999
                ),
                LLMUsage(ts=now - timedelta(days=30), kind="chat", model="m", total_tokens=1),
            ]
        )
        await s.commit()
    daily = (await client.get("/api/usage/daily", params={"days": 7})).json()
    assert daily["days"] == 7
    assert {(r["kind"], r["total_tokens"]) for r in daily["rows"]} == {
        ("summarize", 30),
        ("chat", 15),
    }
    feeds = (await client.get("/api/usage/by-feed")).json()["feeds"]
    assert [(f["title"], f["total_tokens"]) for f in feeds] == [
        ("Feed F", 30),
        ("(no source)", 11),
        ("(deleted feed)", 5),
    ]

    await client.post("/api/users", json={"username": "reader", "password": "readerpass1"})
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "reader", "password": "readerpass1"})
    assert (await client.get("/api/usage/daily")).status_code == 403
    assert (await client.get("/api/usage/by-feed")).status_code == 403
