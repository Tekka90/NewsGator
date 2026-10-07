from httpx import AsyncClient
from tests.conftest import setup_admin

from app.models import Article, Feed, Story, UserFeed


async def test_counts_match_list_filters(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    async with db_session() as s:
        feed = Feed(url="https://counts.example.com/rss", title="C")
        s.add(feed)
        await s.flush()
        s.add(UserFeed(user_id=1, feed_id=feed.id))
        ids = []
        for i, category in enumerate(["Tech", "Tech", "World"]):
            story = Story(title=f"S{i}", summary="x", version=1, category=category)
            s.add(story)
            await s.flush()
            ids.append(story.id)
            s.add(
                Article(
                    feed_id=feed.id,
                    guid=f"count-{i}",
                    url=f"https://c{i}.example.com/{i}",
                    title=f"A{i}",
                    story_id=story.id,
                    processing_state="clustered",
                )
            )
        await s.commit()

    await client.post(f"/api/stories/{ids[0]}/read")
    await client.put(f"/api/stories/{ids[1]}/saved")

    counts = (await client.get("/api/stories/counts")).json()
    assert counts == {"all": 3, "unread": 2, "updated": 0, "saved": 1}
    for name in ("all", "unread", "updated"):
        listed = (await client.get(f"/api/stories?filter={name}")).json()
        assert len(listed) == counts[name]
    saved = (await client.get("/api/stories?saved=true")).json()
    assert len(saved) == counts["saved"]

    tech = (await client.get("/api/stories/counts?category=Tech")).json()
    assert tech == {"all": 2, "unread": 1, "updated": 0, "saved": 1}
