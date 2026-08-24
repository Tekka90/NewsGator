"""Activity API tests (Milestone 6)."""

from httpx import AsyncClient
from tests.conftest import setup_admin

from app.models import ActivityEvent
from app.services import activity


async def test_recent_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/activity/recent")).status_code == 401


async def test_recent_returns_events_and_queue_depth(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    async with db_session() as s:
        await activity.emit(s, "ingest", "feed_poll_done", {"feed": "X", "new_articles": 3})
        await activity.emit(s, "llm", "summarize_done", {"article_id": 1}, level="info")
        await s.commit()

    r = await client.get("/api/activity/recent")
    assert r.status_code == 200
    body = r.json()
    assert "llm_queue_depth" in body
    actions = [e["action"] for e in body["events"]]
    assert "feed_poll_done" in actions and "summarize_done" in actions
    ev = next(e for e in body["events"] if e["action"] == "feed_poll_done")
    assert ev["detail"]["new_articles"] == 3


async def test_recent_component_filter(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    async with db_session() as s:
        await activity.emit(s, "ingest", "feed_poll_done", {})
        await activity.emit(s, "cluster", "cluster_new", {})
        await s.commit()

    r = await client.get("/api/activity/recent?component=cluster")
    actions = [e["action"] for e in r.json()["events"]]
    assert actions == ["cluster_new"]


async def test_sse_broadcast_reaches_subscriber(db_session) -> None:
    """The SSE endpoint reads from activity.subscribe(); test the broadcast seam."""
    import asyncio

    q = activity.subscribe()
    try:
        async with db_session() as s:
            await activity.emit(s, "cluster", "cluster_new", {"story_id": 7})
            await s.commit()
        payload = await asyncio.wait_for(q.get(), timeout=1)
        assert payload["action"] == "cluster_new"
        assert payload["detail"]["story_id"] == 7
    finally:
        activity.unsubscribe(q)


async def test_prune_ring_buffer(db_session) -> None:
    async with db_session() as s:
        for i in range(10):
            await activity.emit(s, "ingest", "feed_poll_done", {"i": i})
        await s.commit()
        monkeymax = activity.RING_BUFFER_MAX
        activity.RING_BUFFER_MAX = 5
        try:
            await activity.prune(s)
            await s.commit()
        finally:
            activity.RING_BUFFER_MAX = monkeymax
        from sqlalchemy import func, select

        count = await s.scalar(select(func.count(ActivityEvent.id)))
        assert count is not None and count <= 5
