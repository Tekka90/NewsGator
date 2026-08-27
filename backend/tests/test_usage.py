"""LLM usage metrics: capture layer (services/usage.py) + /api/usage endpoints."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from tests.conftest import setup_admin

from app.models import Article, Category, Feed, LLMUsage
from app.services import llm_client, process, usage


async def _count(db_session) -> int:
    async with db_session() as s:
        return int(await s.scalar(select(func.count(LLMUsage.id))))


async def _make_article(db_session) -> tuple[int, int]:
    """Returns (article_id, feed_id) for an article ready to summarize."""
    async with db_session() as s:
        s.add(Category(name="Tech"))
        feed = Feed(url="https://f.example.com/rss", title="Feed")
        s.add(feed)
        await s.flush()
        article = Article(
            feed_id=feed.id,
            guid="a1",
            url="https://news.example.com/one",
            title="Some news",
            raw_content="",
            full_text="A long article body about things. " * 20,
            processing_state="fulltext",
        )
        s.add(article)
        await s.commit()
        return article.id, feed.id


async def test_record_uses_server_usage(db_session) -> None:
    """When llm_client captured a `usage` object, tokens are stored as-is."""
    async with db_session() as s:
        llm_client.last_usage.set(
            {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "cached_tokens": 12,
                "reasoning_tokens": None,
            }
        )
        usage.record(
            s, "summarize", endpoint="chat", model="m1", latency_ms=800, prompt_chars=480
        )
        await s.commit()
        row = (await s.scalars(select(LLMUsage))).one()
        assert row.prompt_tokens == 120
        assert row.completion_tokens == 30
        assert row.total_tokens == 150
        assert row.cached_tokens == 12
        assert row.estimated is False
        assert row.latency_ms == 800


async def test_record_estimates_when_usage_missing(db_session) -> None:
    """No `usage` from the server → chars/4 heuristic, flagged estimated."""
    async with db_session() as s:
        llm_client.last_usage.set(None)
        usage.record(
            s,
            "embed",
            endpoint="embed",
            model="e1",
            latency_ms=50,
            prompt_chars=400,
        )
        await s.commit()
        row = (await s.scalars(select(LLMUsage))).one()
        assert row.prompt_tokens == 100  # 400 chars / 4
        assert row.estimated is True


async def test_summarize_article_records_usage(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline integration: summarize_article appends a usage row with the
    article + denormalized feed id."""
    article_id, feed_id = await _make_article(db_session)

    async def fake_chat_json(system: str, user: str, *, model=None):
        llm_client.last_usage.set(
            {
                "prompt_tokens": 500,
                "completion_tokens": 80,
                "total_tokens": 580,
                "cached_tokens": None,
                "reasoning_tokens": None,
            }
        )
        return {"summary": "A summary.", "category": "Tech"}, 1234

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)

    async with db_session() as s:
        article = await s.get(Article, article_id)
        assert article is not None
        assert await process.summarize_article(s, article) is True

    async with db_session() as s:
        row = (await s.scalars(select(LLMUsage))).one()
        assert row.kind == "summarize"
        assert row.endpoint == "chat"
        assert row.article_id == article_id
        assert row.feed_id == feed_id
        assert row.total_tokens == 580
        assert row.latency_ms == 1234


async def _seed_rows(db_session) -> None:
    """Three rows: two today (one estimated, one for a feed), one 40 days ago."""
    now = datetime.now(UTC)
    async with db_session() as s:
        feed = Feed(url="https://f.example.com/rss", title="Example Feed")
        s.add(feed)
        await s.flush()
        s.add_all(
            [
                LLMUsage(
                    ts=now,
                    kind="summarize",
                    endpoint="chat",
                    model="m1",
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                    latency_ms=1000,
                    feed_id=feed.id,
                    article_id=1,
                ),
                LLMUsage(
                    ts=now,
                    kind="embed",
                    endpoint="embed",
                    model="e1",
                    prompt_tokens=400,
                    total_tokens=400,
                    latency_ms=200,
                    estimated=True,
                    feed_id=feed.id,
                    article_id=1,
                ),
                LLMUsage(
                    ts=now - timedelta(days=40),
                    kind="share_translate",
                    endpoint="chat",
                    model="m1",
                    prompt_tokens=60,
                    completion_tokens=40,
                    total_tokens=100,
                    latency_ms=500,
                ),
            ]
        )
        await s.commit()


async def test_usage_endpoints_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/usage/summary")).status_code == 401
    assert (await client.get("/api/usage/daily")).status_code == 401
    assert (await client.get("/api/usage/by-feed")).status_code == 401


async def test_usage_summary_periods(client: AsyncClient, db_session) -> None:
    await _seed_rows(db_session)
    await setup_admin(client)

    r = await client.get("/api/usage/summary?period=all")
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["calls"] == 3
    assert body["totals"]["total_tokens"] == 650
    assert body["totals"]["estimated_calls"] == 1
    kinds = {k["kind"]: k for k in body["by_kind"]}
    # chat throughput: 50 completion tokens over 1000 ms = 50 tok/s
    assert kinds["summarize"]["tokens_per_s"] == 50.0
    # embed throughput: 400 prompt tokens over 200 ms = 2000 tok/s
    assert kinds["embed"]["tokens_per_s"] == 2000.0

    r = await client.get("/api/usage/summary?period=day")
    assert r.json()["totals"]["calls"] == 2  # the 40-day-old row is excluded


async def test_usage_daily_and_by_feed(client: AsyncClient, db_session) -> None:
    await _seed_rows(db_session)
    await setup_admin(client)

    r = await client.get("/api/usage/daily?days=90")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 3  # 2 kinds today + 1 row 40 days ago
    today = datetime.now(UTC).date().isoformat()
    today_rows = [row for row in rows if row["day"] == today]
    assert {row["kind"] for row in today_rows} == {"summarize", "embed"}

    r = await client.get("/api/usage/by-feed")
    feeds = r.json()["feeds"]
    by_title = {f["title"]: f for f in feeds}
    assert by_title["Example Feed"]["total_tokens"] == 550
    # share_translate has no article → no feed attribution
    assert by_title["(no source)"]["total_tokens"] == 100
