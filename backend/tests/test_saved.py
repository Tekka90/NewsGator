from datetime import UTC, datetime

from httpx import AsyncClient
from tests.conftest import setup_admin

from app.models import Article, Feed, Story, StoryState, UserFeed


async def _login(client: AsyncClient, username: str, password: str) -> None:
    await client.post("/api/auth/logout")
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


async def test_saved_endpoints_are_idempotent_and_do_not_mark_read(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    async with db_session() as s:
        feed = Feed(url="https://saved.example.com/rss", title="Saved")
        story = Story(title="Saved Story", summary="Summary", version=3)
        s.add_all([feed, story])
        await s.flush()
        s.add_all(
            [
                UserFeed(user_id=1, feed_id=feed.id),
                Article(
                    feed_id=feed.id,
                    guid="saved-1",
                    url="https://saved.example.com/1",
                    title="Saved article",
                    story_id=story.id,
                    processing_state="clustered",
                ),
            ]
        )
        await s.commit()
        story_id = story.id

    response = await client.put(f"/api/stories/{story_id}/saved")
    assert response.status_code == 200
    first = response.json()
    assert first["saved"] is True
    assert first["saved_at"] is not None

    response = await client.put(f"/api/stories/{story_id}/saved")
    assert response.status_code == 200
    assert response.json() == first

    stories = await client.get("/api/stories?saved=true&filter=unread")
    assert stories.status_code == 200
    assert [story["id"] for story in stories.json()] == [story_id]
    assert stories.json()[0]["saved"] is True
    assert stories.json()[0]["saved_at"] == first["saved_at"]
    assert stories.json()[0]["is_read"] is False

    detail = await client.get(f"/api/stories/{story_id}")
    assert detail.status_code == 200
    assert detail.json()["saved"] is True
    assert detail.json()["saved_at"] == first["saved_at"]
    assert detail.json()["is_read"] is False

    async with db_session() as s:
        state = await s.get(StoryState, (1, story_id))
        assert state is not None
        assert state.is_read is False
        assert state.read_at_version == 0
        assert state.read_at is None
        assert state.saved_at is not None

    response = await client.delete(f"/api/stories/{story_id}/saved")
    assert response.status_code == 200
    assert response.json() == {"saved": False, "saved_at": None}

    response = await client.delete(f"/api/stories/{story_id}/saved")
    assert response.status_code == 200
    assert response.json() == {"saved": False, "saved_at": None}

    stories = await client.get("/api/stories?saved=true")
    assert stories.status_code == 200
    assert stories.json() == []

    async with db_session() as s:
        state = await s.get(StoryState, (1, story_id))
        assert state is not None
        assert state.is_read is False
        assert state.read_at_version == 0
        assert state.read_at is None
        assert state.saved_at is None


async def test_saved_flag_is_per_user_and_requires_story_visibility(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    response = await client.post(
        "/api/users", json={"username": "reader", "password": "readerpass"}
    )
    assert response.status_code == 201
    reader_id = response.json()["id"]
    response = await client.post(
        "/api/users", json={"username": "hidden", "password": "hiddenpass"}
    )
    assert response.status_code == 201

    async with db_session() as s:
        feed = Feed(url="https://per-user.example.com/rss", title="Shared")
        story = Story(title="Shared Story", summary="Summary")
        s.add_all([feed, story])
        await s.flush()
        s.add_all(
            [
                UserFeed(user_id=1, feed_id=feed.id),
                UserFeed(user_id=reader_id, feed_id=feed.id),
                Article(
                    feed_id=feed.id,
                    guid="shared-1",
                    url="https://per-user.example.com/1",
                    title="Shared article",
                    story_id=story.id,
                    processing_state="clustered",
                ),
            ]
        )
        await s.commit()
        story_id = story.id

    assert (await client.put(f"/api/stories/{story_id}/saved")).status_code == 200

    await _login(client, "reader", "readerpass")
    detail = await client.get(f"/api/stories/{story_id}")
    assert detail.status_code == 200
    assert detail.json()["saved"] is False
    stories = await client.get("/api/stories?saved=true")
    assert stories.status_code == 200
    assert stories.json() == []

    await _login(client, "hidden", "hiddenpass")
    assert (await client.put(f"/api/stories/{story_id}/saved")).status_code == 404
    assert (await client.delete(f"/api/stories/{story_id}/saved")).status_code == 404

    await _login(client, "admin", "supersecret1")
    detail = await client.get(f"/api/stories/{story_id}")
    assert detail.status_code == 200
    assert detail.json()["saved"] is True


async def test_saved_filter_composes_with_updated_filter(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    old = datetime.now(UTC)
    async with db_session() as s:
        feed = Feed(url="https://filter.example.com/rss", title="Filter")
        saved_story = Story(title="Saved updated", summary="Summary", version=3)
        other_story = Story(title="Unsaved updated", summary="Summary", version=3)
        s.add_all([feed, saved_story, other_story])
        await s.flush()
        s.add(UserFeed(user_id=1, feed_id=feed.id))
        s.add_all(
            [
                Article(
                    feed_id=feed.id,
                    guid="saved-updated",
                    url="https://filter.example.com/saved",
                    title="Saved updated",
                    story_id=saved_story.id,
                    processing_state="clustered",
                    fetched_at=old,
                ),
                Article(
                    feed_id=feed.id,
                    guid="unsaved-updated",
                    url="https://filter.example.com/other",
                    title="Unsaved updated",
                    story_id=other_story.id,
                    processing_state="clustered",
                    fetched_at=old,
                ),
                StoryState(
                    user_id=1,
                    story_id=saved_story.id,
                    is_read=True,
                    read_at_version=2,
                    saved_at=old,
                ),
                StoryState(
                    user_id=1, story_id=other_story.id, is_read=True, read_at_version=2
                ),
            ]
        )
        await s.commit()

    response = await client.get("/api/stories?filter=updated&saved=true")
    assert response.status_code == 200
    body = response.json()
    assert [story["title"] for story in body] == ["Saved updated"]
    assert body[0]["saved"] is True
    assert body[0]["saved_at"] is not None
