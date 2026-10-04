from httpx import AsyncClient
from tests.conftest import setup_admin

from app.models import Article, Feed, Story, UserFeed


async def test_facets_count_categories_and_feeds(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    async with db_session() as s:
        feeds = [Feed(url=f"https://facets{i}.example.com/rss", title=f"F{i}") for i in range(2)]
        s.add_all(feeds)
        await s.flush()
        for f in feeds:
            s.add(UserFeed(user_id=1, feed_id=f.id))
        layout = [("Tech", 0), ("Tech", 0), ("Tech", 1), ("World", 1)]
        for index, (category, feed_index) in enumerate(layout):
            story = Story(title=f"S{index}", summary="x", version=1, category=category)
            s.add(story)
            await s.flush()
            s.add(
                Article(
                    feed_id=feeds[feed_index].id,
                    guid=f"facet-{index}",
                    url=f"https://h{index}.example.com/{index}",
                    title=f"A{index}",
                    story_id=story.id,
                    processing_state="clustered",
                )
            )
        await s.commit()
        feed_ids = [f.id for f in feeds]

    body = (await client.get("/api/stories/facets?filter=unread")).json()
    assert {c["key"]: c["count"] for c in body["categories"]} == {"Tech": 3, "World": 1}
    assert {f["key"]: f["count"] for f in body["feeds"]} == {
        str(feed_ids[0]): 2,
        str(feed_ids[1]): 2,
    }

    scoped = (await client.get("/api/stories/facets?category=World")).json()
    assert {f["key"]: f["count"] for f in scoped["feeds"]} == {str(feed_ids[1]): 1}
    by_feed = (await client.get(f"/api/stories/facets?feed={feed_ids[0]}")).json()
    assert {c["key"]: c["count"] for c in by_feed["categories"]} == {"Tech": 2}
