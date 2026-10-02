from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from tests.conftest import setup_admin

from app.models import Article, Feed, Story, UserFeed


async def _seed(db_session, count: int) -> list[int]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with db_session() as s:
        feed = Feed(url="https://paging.example.com/rss", title="Paging")
        s.add(feed)
        await s.flush()
        s.add(UserFeed(user_id=1, feed_id=feed.id))
        ids: list[int] = []
        for index in range(count):
            story = Story(title=f"Story {index}", summary="Summary", version=1)
            s.add(story)
            await s.flush()
            s.add(
                Article(
                    feed_id=feed.id,
                    guid=f"paging-{index}",
                    url=f"https://www.host{index}.example.com/{index}",
                    title=f"Article {index}",
                    story_id=story.id,
                    published_at=base + timedelta(hours=index),
                    processing_state="clustered",
                )
            )
            ids.append(story.id)
        await s.commit()
    return ids


async def test_limit_and_offset_slice_the_sorted_list(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, 6)

    full = await client.get("/api/stories?filter=unread&sort=published&order=asc")
    assert full.status_code == 200
    assert [story["id"] for story in full.json()] == ids

    page = await client.get("/api/stories?filter=unread&sort=published&order=asc&limit=2&offset=1")
    assert [story["id"] for story in page.json()] == ids[1:3]
    assert page.json()[0]["source_hosts"] == ["host1.example.com"]
    assert page.json()[0]["source_count"] == 1

    beyond = await client.get("/api/stories?limit=5&offset=10")
    assert beyond.json() == []


async def test_paged_descending_order_matches_the_full_list(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    await _seed(db_session, 5)
    full = (await client.get("/api/stories?sort=published&order=desc")).json()
    page = (await client.get("/api/stories?sort=published&order=desc&limit=3")).json()
    assert [story["id"] for story in page] == [story["id"] for story in full][:3]


async def test_invalid_paging_is_rejected(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    assert (await client.get("/api/stories?limit=0")).status_code == 422
    assert (await client.get("/api/stories?offset=-1")).status_code == 422


async def test_since_ranks_stories_by_recent_sources(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    now = datetime.now(UTC)
    async with db_session() as s:
        feed = Feed(url="https://since.example.com/rss", title="Since")
        s.add(feed)
        await s.flush()
        s.add(UserFeed(user_id=1, feed_id=feed.id))
        # (recent articles, old articles) per story
        layouts = {"busy-old": (1, 5), "hot": (3, 0), "warm": (2, 0), "stale": (0, 4)}
        ids: dict[str, int] = {}
        for name, (recent, old) in layouts.items():
            story = Story(title=name, summary="Summary", version=1)
            s.add(story)
            await s.flush()
            ids[name] = story.id
            for index in range(recent + old):
                s.add(
                    Article(
                        feed_id=feed.id,
                        guid=f"{name}-{index}",
                        url=f"https://{name}{index}.example.com/",
                        title=name,
                        story_id=story.id,
                        published_at=now - timedelta(hours=1 if index < recent else 72),
                        processing_state="clustered",
                    )
                )
        await s.commit()

    since = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    response = await client.get(
        "/api/stories", params={"sort": "sources", "order": "desc", "since": since}
    )
    assert response.status_code == 200
    assert [story["title"] for story in response.json()] == ["hot", "warm", "busy-old"]

    everything = await client.get("/api/stories", params={"sort": "sources", "order": "desc"})
    assert everything.json()[0]["title"] == "busy-old"
    assert (await client.get("/api/stories?since=nonsense")).status_code == 422


async def test_widget_snapshot_returns_every_list_in_one_call(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, 5)
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    response = await client.get("/api/stories/widget-snapshot", params={"since": since, "limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["unread_count"] == 5
    assert [s["id"] for s in body["lists"]["latest"]] == [ids[4], ids[3]]
    assert [s["id"] for s in body["lists"]["oldest_unread"]] == [ids[0], ids[1]]
    assert body["lists"]["most_sources"] == []  # the seeded articles are older than the window
    assert body["lists"]["latest"][0]["source_hosts"] == ["host4.example.com"]

    assert (await client.get("/api/stories/widget-snapshot")).status_code == 422
