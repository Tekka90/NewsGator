"""Coverage tests: scheduler, qdrant store, llm client, readeck, saved links."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.models import Article, Feed, Story, StoryState, User, UserFeed
from app.services import llm_client, qdrant_store, readeck, saved_links, youtube
from app.workers import scheduler

# --- scheduler ---


async def test_poll_due_feeds_polls_only_enabled_due_rss(db_session, monkeypatch) -> None:
    now = datetime.now(UTC)
    async with db_session() as s:
        s.add_all(
            [
                Feed(url="https://a.test/rss", kind="rss"),
                Feed(url="https://b.test/rss", kind="rss", is_enabled=False),
                Feed(url="mail:1", kind="mail"),
                Feed(url="https://c.test/rss", kind="rss", last_fetched_at=now),
            ]
        )
        await s.commit()
    polled: list[str] = []

    async def fake_poll(session, feed):
        polled.append(feed.url)
        return 0

    monkeypatch.setattr(scheduler, "poll_feed", fake_poll)
    await scheduler.poll_due_feeds()
    assert polled == ["https://a.test/rss"]


async def test_freeze_sweep_freezes_old_stories(db_session) -> None:
    old = datetime.now(UTC) - timedelta(hours=settings.freeze_after_hours + 5)
    async with db_session() as s:
        s.add_all([Story(title="old", first_seen_at=old), Story(title="new")])
        await s.commit()
    await scheduler.freeze_sweep()
    async with db_session() as s:
        stories = {st.title: st.is_frozen for st in (await s.scalars(select(Story)))}
    assert stories == {"old": True, "new": False}


async def test_llm_backlog_sweep_requeues_and_logs(db_session, monkeypatch) -> None:
    from app.models import ActivityEvent
    from app.services import process

    async with db_session() as s:
        feed = Feed(url="https://a.test/rss")
        s.add(feed)
        await s.flush()
        s.add(
            Article(feed_id=feed.id, guid="g", url="https://a.test/1", processing_state="fetched")
        )
        await s.commit()
    queued: list[int] = []
    monkeypatch.setattr(process, "enqueue_article", lambda i: queued.append(i))
    await scheduler.llm_backlog_sweep()
    assert queued == [1]
    async with db_session() as s:
        ev = await s.scalar(select(ActivityEvent).where(ActivityEvent.action == "backlog_sweep"))
    assert ev is not None

    # nothing left to requeue -> no event emitted
    async with db_session() as s:
        for a in await s.scalars(select(Article)):
            a.processing_state = "clustered"
        await s.commit()
        for e in await s.scalars(select(ActivityEvent)):
            await s.delete(e)
        await s.commit()
    await scheduler.llm_backlog_sweep()
    async with db_session() as s:
        assert await s.scalar(select(ActivityEvent)) is None


async def test_mail_reader_and_retention_sweeps_delegate(db_session, monkeypatch) -> None:
    from app.services import mailnews, retention
    from app.services.readers import sync

    calls: list[str] = []

    async def mail() -> None:
        calls.append("mail")

    async def reader() -> None:
        calls.append("reader")

    async def purge(session) -> dict[str, int]:
        calls.append("purge")
        return {}

    monkeypatch.setattr(mailnews, "poll_all_accounts", mail)
    monkeypatch.setattr(sync, "poll_all_reader_accounts", reader)
    monkeypatch.setattr(scheduler, "purge_old_data", purge)
    assert retention.purge_old_data  # real one untouched elsewhere
    await scheduler.mail_poll_sweep()
    await scheduler.reader_poll_sweep()
    await scheduler.retention_sweep()
    assert calls == ["mail", "reader", "purge"]


async def test_start_and_stop_scheduler_registers_jobs(monkeypatch) -> None:
    added: list[dict[str, Any]] = []
    state = {"started": False, "stopped": False}
    fake = SimpleNamespace(
        add_job=lambda fn, **kw: added.append({"fn": fn, **kw}),
        start=lambda: state.__setitem__("started", True),
        shutdown=lambda wait: state.__setitem__("stopped", wait is False),
        running=True,
    )
    monkeypatch.setattr(scheduler, "scheduler", fake)
    scheduler.start_scheduler()
    assert state["started"]
    assert {j["id"] for j in added} == {
        "poll_due_feeds",
        "freeze_sweep",
        "llm_backlog_sweep",
        "mail_poll_sweep",
        "reader_poll_sweep",
        "retention_sweep",
    }
    assert all(j["max_instances"] == 1 and j["coalesce"] for j in added)
    scheduler.stop_scheduler()
    assert state["stopped"]
    fake.running = False
    state["stopped"] = False
    scheduler.stop_scheduler()
    assert not state["stopped"]


# --- qdrant store ---


class _FakeQdrant:
    def __init__(self, existing: dict[str, int] | None = None) -> None:
        self.existing = existing or {}
        self.created: list[tuple[str, int]] = []
        self.upserts: list[tuple[str, Any]] = []
        self.deleted: list[tuple[str, Any]] = []
        self.points: list[Any] = []

    async def collection_exists(self, name: str) -> bool:
        return name in self.existing

    async def get_collection(self, name: str) -> Any:
        size = self.existing[name]
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=size)))
        )

    async def create_collection(self, name: str, vectors_config: Any) -> None:
        self.created.append((name, vectors_config.size))

    async def upsert(self, name: str, points: list[Any]) -> None:
        self.upserts.append((name, points))

    async def retrieve(self, name: str, ids: list[int], with_vectors: bool) -> list[Any]:
        return self.points

    async def query_points(self, name: str, query: list[float], limit: int) -> Any:
        return SimpleNamespace(
            points=[SimpleNamespace(id=7, score=0.9), SimpleNamespace(id=8, score=0.5)]
        )

    async def delete(self, name: str, points_selector: list[int]) -> None:
        self.deleted.append((name, points_selector))


@pytest.fixture()
def qstore(monkeypatch) -> tuple[qdrant_store.QdrantVectorStore, _FakeQdrant]:
    fake = _FakeQdrant()
    monkeypatch.setattr(settings, "qdrant_url", "http://q.test:6333")
    monkeypatch.setattr(qdrant_store, "AsyncQdrantClient", lambda **kw: fake)
    return qdrant_store.QdrantVectorStore(), fake


def _embed_dim(monkeypatch, dim: int) -> None:
    async def fake_embed(texts, model=None):
        return [[0.0] * dim for _ in texts]

    monkeypatch.setattr(llm_client, "embed", fake_embed)


def test_qdrant_requires_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "qdrant_url", "")
    with pytest.raises(RuntimeError, match="QDRANT_URL"):
        qdrant_store.QdrantVectorStore()


async def test_qdrant_ensure_collections_dimension_mismatch(qstore, monkeypatch) -> None:
    store, fake = qstore
    fake.existing = {qdrant_store.ARTICLES: 512}
    _embed_dim(monkeypatch, 8)
    with pytest.raises(RuntimeError, match="has dim 512"):
        await store.ensure_collections()


async def test_qdrant_ensure_collections_keeps_matching_and_creates_missing(
    qstore, monkeypatch
) -> None:
    store, fake = qstore
    fake.existing = {qdrant_store.ARTICLES: 8}
    _embed_dim(monkeypatch, 8)
    await store.ensure_collections()
    assert fake.created == [(qdrant_store.STORIES, 8)]


async def test_qdrant_upsert_retrieve_search_delete(qstore) -> None:
    store, fake = qstore
    await store.upsert_article(1, [0.1, 0.2])
    await store.upsert_story_centroid(2, [0.3])
    assert [(n, p[0].id) for n, p in fake.upserts] == [
        (qdrant_store.ARTICLES, 1),
        (qdrant_store.STORIES, 2),
    ]

    fake.points = []
    assert await store.get_article_vector(1) is None
    fake.points = [SimpleNamespace(vector=None)]
    assert await store.get_story_centroid(2) is None
    fake.points = [SimpleNamespace(vector=[1, 2.5])]
    assert await store.get_article_vector(1) == [1.0, 2.5]
    fake.points = [SimpleNamespace(vector={"named": [1.0]})]  # multi-vector shape rejected
    assert await store.get_story_centroid(2) is None

    assert await store.search_story_centroids([0.1]) == []  # collection missing
    fake.existing = {qdrant_store.STORIES: 2}
    assert await store.search_story_centroids([0.1], limit=2) == [(7, 0.9), (8, 0.5)]

    await store.delete_article(1)
    await store.delete_story(2)
    assert fake.deleted == [(qdrant_store.ARTICLES, [1]), (qdrant_store.STORIES, [2])]


# --- llm client ---


def _resp(content: str, usage: Any = None) -> Any:
    msg = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def _client(create) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_extract_usage_details_and_absent() -> None:
    assert llm_client._extract_usage(SimpleNamespace()) is None
    u = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )
    assert llm_client._extract_usage(SimpleNamespace(usage=u)) == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 4,
        "reasoning_tokens": 2,
    }
    bare = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    out = llm_client._extract_usage(SimpleNamespace(usage=bare))
    assert out is not None and out["cached_tokens"] is None and out["reasoning_tokens"] is None


def test_is_configured_and_clients(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", "")
    assert not llm_client.is_configured()
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "embed_base_url", "")
    assert llm_client.is_configured()
    assert str(llm_client._chat_client().base_url).startswith("http://llm.test/v1")
    assert str(llm_client._embed_client().base_url).startswith("http://llm.test/v1")
    monkeypatch.setattr(settings, "embed_base_url", "http://embed.test/v1")
    assert str(llm_client._embed_client().base_url).startswith("http://embed.test/v1")


async def test_chat_json_captures_usage(monkeypatch) -> None:
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)

    async def create(**kw):
        return _resp('{"a": 1}', usage)

    monkeypatch.setattr(llm_client, "_chat_client", lambda: _client(create))
    parsed, _ = await llm_client.chat_json("s", "u")
    assert parsed == {"a": 1}
    assert llm_client.last_usage.get()["total_tokens"] == 5  # type: ignore[index]


async def test_chat_json_rejects_non_object_after_retry(monkeypatch) -> None:
    calls = 0
    seen: list[list[Any]] = []

    async def create(**kw):
        nonlocal calls
        calls += 1
        seen.append(list(kw["messages"]))
        return _resp(json.dumps([1, 2]))

    monkeypatch.setattr(llm_client, "_chat_client", lambda: _client(create))
    with pytest.raises(llm_client.LLMError, match="invalid JSON after retry"):
        await llm_client.chat_json("s", "u")
    assert calls == 2
    assert "ONLY a valid JSON" in seen[1][-1]["content"]


async def test_chat_json_transport_error_not_retried(monkeypatch) -> None:
    calls = 0

    async def create(**kw):
        nonlocal calls
        calls += 1
        raise ConnectionError("down")

    monkeypatch.setattr(llm_client, "_chat_client", lambda: _client(create))
    with pytest.raises(llm_client.LLMError, match="request failed: down"):
        await llm_client.chat_json("s", "u")
    assert calls == 1


async def test_chat_text_returns_content_and_wraps_errors(monkeypatch) -> None:
    kwargs_seen: dict[str, Any] = {}

    async def ok(**kw):
        kwargs_seen.update(kw)
        return _resp(
            "free form", SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        )

    monkeypatch.setattr(llm_client, "_chat_client", lambda: _client(ok))
    text, latency = await llm_client.chat_text("s", "u", model="m1")
    assert text == "free form" and latency >= 0
    assert kwargs_seen["model"] == "m1" and "response_format" not in kwargs_seen

    async def boom(**kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(llm_client, "_chat_client", lambda: _client(boom))
    with pytest.raises(llm_client.LLMError, match="request failed: nope"):
        await llm_client.chat_text("s", "u")


async def test_embed_success_and_failure(monkeypatch) -> None:
    async def create(model, input):
        data = [SimpleNamespace(embedding=(0.5, 1.5)) for _ in input]
        return SimpleNamespace(data=data, usage=None)

    emb = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    monkeypatch.setattr(llm_client, "_embed_client", lambda: emb)
    assert await llm_client.embed(["a", "b"]) == [[0.5, 1.5], [0.5, 1.5]]
    assert llm_client.last_usage.get() is None
    assert await llm_client.embed([]) == []

    async def boom(model, input):
        raise OSError("refused")

    emb2 = SimpleNamespace(embeddings=SimpleNamespace(create=boom))
    monkeypatch.setattr(llm_client, "_embed_client", lambda: emb2)
    with pytest.raises(llm_client.LLMError, match="Embedding request failed: refused"):
        await llm_client.embed(["x"])


async def test_test_connection_reports_each_leg(monkeypatch) -> None:
    async def chat_fail(system, user, **kw):
        raise llm_client.LLMError("chat dead")

    async def embed_ok(texts, **kw):
        return [[0.0]]

    monkeypatch.setattr(llm_client, "chat_json", chat_fail)
    monkeypatch.setattr(llm_client, "embed", embed_ok)
    res = await llm_client.test_connection()
    assert res == {"chat": False, "embeddings": True, "errors": ["chat dead"]}

    async def chat_ok(system, user, **kw):
        return {"ok": True}, 1

    async def embed_fail(texts, **kw):
        raise llm_client.LLMError("embed dead")

    monkeypatch.setattr(llm_client, "chat_json", chat_ok)
    monkeypatch.setattr(llm_client, "embed", embed_fail)
    res = await llm_client.test_connection()
    assert res == {"chat": True, "embeddings": False, "errors": ["embed dead"]}


# --- readeck ---


class _FakeHttp:
    """Stand-in for httpx.AsyncClient capturing the POST."""

    response: Any = None
    error: Exception | None = None
    sent: dict[str, Any] = {}  # noqa: RUF012

    def __init__(self, **kw: Any) -> None: ...

    async def __aenter__(self) -> "_FakeHttp":
        return self

    async def __aexit__(self, *a: Any) -> None: ...

    async def post(self, url: str, **kw: Any) -> Any:
        type(self).sent = {"url": url, **kw}
        if type(self).error:
            raise type(self).error
        return type(self).response


def _http_response(status: int, headers: dict[str, str] | None = None, text: str = "") -> Any:
    return SimpleNamespace(status_code=status, headers=headers or {}, text=text)


async def test_readeck_post_bookmark_success(monkeypatch) -> None:
    monkeypatch.setattr(settings, "readeck_base_url", "http://rd.test/")
    monkeypatch.setattr(settings, "readeck_token", "tok")
    monkeypatch.setattr(readeck.httpx, "AsyncClient", _FakeHttp)
    _FakeHttp.error = None
    _FakeHttp.response = _http_response(202, {"bookmark-id": "abc"})
    out = await readeck._post_bookmark(
        url="https://x/1",
        title="T",
        html="<p>x</p>",
        labels=["newsgator", "tech"],
        created="2024-01-01",
    )
    assert out == {"bookmark_id": "abc", "href": "http://rd.test/api/bookmarks/abc"}
    sent = _FakeHttp.sent
    assert sent["url"] == "http://rd.test/api/bookmarks"
    assert sent["headers"]["Authorization"] == "Bearer tok"
    assert sent["data"] == {"url": "https://x/1", "title": "T", "created": "2024-01-01"}
    assert [f[0] for f in sent["files"]] == ["html", "labels", "labels"]

    _FakeHttp.response = _http_response(200, {"bookmark-id": "z", "location": "http://rd.test/b/z"})
    out = await readeck._post_bookmark(url="u", title="t", html="h", labels=[], created=None)
    assert out["href"] == "http://rd.test/b/z"
    assert "created" not in _FakeHttp.sent["data"]


async def test_readeck_post_bookmark_errors(monkeypatch) -> None:
    monkeypatch.setattr(settings, "readeck_base_url", "http://rd.test")
    monkeypatch.setattr(settings, "readeck_token", "tok")
    monkeypatch.setattr(readeck.httpx, "AsyncClient", _FakeHttp)
    _FakeHttp.error = None
    _FakeHttp.response = _http_response(401, text="denied")
    with pytest.raises(readeck.ReadeckError, match="HTTP 401: denied"):
        await readeck._post_bookmark(url="u", title="t", html="h", labels=[], created=None)
    _FakeHttp.error = httpx.ConnectError("refused")
    with pytest.raises(readeck.ReadeckError, match="request failed"):
        await readeck._post_bookmark(url="u", title="t", html="h", labels=[], created=None)
    _FakeHttp.error = None


async def test_readeck_save_story_picks_primary_and_validates(monkeypatch) -> None:
    monkeypatch.setattr(settings, "readeck_base_url", None)
    monkeypatch.setattr(settings, "readeck_token", None)
    story = Story(id=1, title="Title", summary="p1\n\np2", category="Tech", image_url=None)
    with pytest.raises(readeck.ReadeckError, match="not configured"):
        await readeck.save_story(story, [])
    monkeypatch.setattr(settings, "readeck_base_url", "http://rd.test")
    monkeypatch.setattr(settings, "readeck_token", "tok")
    with pytest.raises(readeck.ReadeckError, match="no articles"):
        await readeck.save_story(story, [])

    seen: dict[str, Any] = {}

    async def fake_post(**kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"bookmark_id": "b", "href": "h"}

    monkeypatch.setattr(readeck, "_post_bookmark", fake_post)
    late = Article(
        id=1, url="https://x/late", title="Late", published_at=datetime(2024, 5, 2, tzinfo=UTC)
    )
    early = Article(
        id=2,
        url="https://x/early",
        title="Early <b>",
        published_at=datetime(2024, 5, 1, tzinfo=UTC),
    )
    undated = Article(id=3, url="https://x/undated", title=None, published_at=None)
    out = await readeck.save_story(story, [late, undated, early])
    assert seen["url"] == "https://x/early"
    assert seen["labels"] == ["newsgator", "tech"]
    assert seen["created"].startswith("2024-05-01")
    assert "<p>p1</p><p>p2</p>" in seen["html"]
    assert "Early &lt;b&gt;" in seen["html"] and "https://x/undated" in seen["html"]
    assert out["bookmark_id"] == "b" and out["latency_ms"] >= 0

    # an article equal to the story's lead image URL wins
    story.image_url = "https://x/late"
    await readeck.save_story(story, [late, early])
    assert seen["url"] == "https://x/late"
    assert '<img src="https://x/late"' in seen["html"]


# --- saved links ---


def test_saved_feed_owner_parsing() -> None:
    assert saved_links.saved_feed_owner(Feed(kind="saved", url="saved:12")) == 12
    assert saved_links.saved_feed_owner(Feed(kind="saved", url="saved:abc")) is None
    assert saved_links.saved_feed_owner(Feed(kind="rss", url="saved:12")) is None
    assert saved_links.saved_feed_owner(Feed(kind="saved", url="other:12")) is None
    assert saved_links.saved_feed_url(3) == "saved:3"


def test_validate_shared_url() -> None:
    assert (
        saved_links.validate_shared_url("  https://A.test/x?utm_source=q  ") == "https://a.test/x"
    )
    for bad in ("ftp://a.test", "https://", "x" * 3000, "https://a.test/" + "x" * 2100):
        with pytest.raises(HTTPException) as ei:
            saved_links.validate_shared_url(bad)
        assert ei.value.status_code == 422


async def _user(db_session) -> User:
    async with db_session() as s:
        u = User(username="u", password_hash="x")
        s.add(u)
        await s.commit()
        return u


async def test_flag_shared_article_story_only_for_owner(db_session) -> None:
    user = await _user(db_session)
    async with db_session() as s:
        saved = Feed(url=saved_links.saved_feed_url(user.id), kind="saved")
        ghost = Feed(url="saved:999", kind="saved")
        rss = Feed(url="https://r.test/rss")
        story = Story(title="s")
        s.add_all([saved, ghost, rss, story])
        await s.flush()
        in_saved = Article(feed_id=saved.id, guid="1", url="https://a/1", story_id=story.id)
        in_ghost = Article(feed_id=ghost.id, guid="2", url="https://a/2", story_id=story.id)
        in_rss = Article(feed_id=rss.id, guid="3", url="https://a/3", story_id=story.id)
        no_story = Article(feed_id=saved.id, guid="4", url="https://a/4")
        s.add_all([in_saved, in_ghost, in_rss, no_story])
        await s.flush()

        for a in (in_ghost, in_rss, no_story):
            await saved_links.flag_shared_article_story(s, a)
        await s.flush()
        assert await s.get(StoryState, (user.id, story.id)) is None

        await saved_links.flag_shared_article_story(s, in_saved)
        await s.flush()
        first = (await s.get(StoryState, (user.id, story.id))).saved_at  # type: ignore[union-attr]
        assert first is not None
        await saved_links.flag_story_saved(s, user.id, story.id)  # idempotent
        assert (await s.get(StoryState, (user.id, story.id))).saved_at == first  # type: ignore[union-attr]


async def test_import_url_existing_article_flags_story_saved(db_session) -> None:
    user = await _user(db_session)
    async with db_session() as s:
        feed = Feed(url="https://r.test/rss")
        story = Story(title="s")
        s.add_all([feed, story])
        await s.flush()
        s.add(UserFeed(user_id=user.id, feed_id=feed.id))
        s.add(Article(feed_id=feed.id, guid="1", url="https://a.test/p", story_id=story.id))
        await s.commit()
        article, known = await saved_links.import_url(s, user, "https://a.test/p?utm_medium=x")
        assert known and article.url == "https://a.test/p"
        state = await s.get(StoryState, (user.id, story.id))
        assert state is not None and state.saved_at is not None


async def test_import_youtube_url_uses_oembed_then_thumbnail(db_session, monkeypatch) -> None:
    user = await _user(db_session)

    async def oembed(url: str):
        return youtube.OEmbed(
            title="Vid title", author_name="Chan", thumbnail_url="https://img/t.jpg"
        )

    monkeypatch.setattr(youtube, "fetch_oembed", oembed)
    async with db_session() as s:
        art, known = await saved_links.import_url(
            s, user, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        assert not known
        assert art.title == "Vid title" and art.origin_feed_title == "Chan"
        assert art.image_url == "https://img/t.jpg"

    async def no_oembed(url: str):
        return None

    monkeypatch.setattr(youtube, "fetch_oembed", no_oembed)
    async with db_session() as s:
        art, _ = await saved_links.import_url(
            s, user, "https://www.youtube.com/watch?v=zzzzzzzzzzz"
        )
        assert art.title == "www.youtube.com"
        assert art.image_url == youtube.thumbnail_url("zzzzzzzzzzz")
        # the saved pseudo feed is reused, not duplicated
        feeds = (await s.scalars(select(Feed).where(Feed.kind == "saved"))).all()
        assert len(feeds) == 1
