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
