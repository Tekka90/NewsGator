"""Stories invisible to the caller (no subscribed feed) are 404 on every story-scoped route."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import ADMIN, setup_admin
from tests.test_cov_api_stories import _seed

from app.api import stories as stories_api
from app.core.config import settings as env_settings
from app.models import Article, Story
from app.services import feed_directory, readeck, share
from app.services.vectorstore import InMemoryVectorStore

OTHER = {"username": "outsider", "password": "outsiderpass1"}


async def _login(client: AsyncClient, creds: dict[str, str]) -> None:
    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, r.text


async def _outsider(client: AsyncClient) -> None:
    assert (await client.post("/api/users", json=OTHER)).status_code in (200, 201)
    await _login(client, OTHER)


async def test_non_subscriber_gets_404_on_story_routes(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    monkeypatch.setattr(env_settings, "readeck_base_url", "http://readeck.test")
    monkeypatch.setattr(env_settings, "readeck_token", "tok")
    assert readeck.is_enabled()
    store = InMemoryVectorStore()
    monkeypatch.setattr(stories_api, "get_vector_store", lambda session=None: store)

    async def fake_post(*args: object, **kwargs: object) -> dict[str, str]:
        return {"bookmark_id": "b1", "href": "http://readeck.test/b1"}

    async def fake_share(story: Story, articles: object, language: object) -> dict[str, object]:
        return {
            "title": story.title, "text": story.summary, "language": "en",
            "translated": False, "latency_ms": 0, "url": "https://x.example.com",
        }

    monkeypatch.setattr(readeck, "_post_bookmark", fake_post)
    monkeypatch.setattr(share, "prepare_share", fake_share)

    v, e, a_v = ids["visible"], ids["extra"], ids["a_visible"]
    await _outsider(client)
    for sid in (v, ids["hidden"]):
        assert (await client.post(f"/api/stories/{sid}/readeck")).status_code == 404
        assert (await client.post(f"/api/stories/{sid}/share", json={})).status_code == 404
        assert (await client.post(f"/api/stories/{sid}/read")).status_code == 404
        assert (await client.get(f"/api/stories/{sid}/diff?from=1")).status_code == 404
        assert (await client.get(f"/api/stories/{sid}/similar")).status_code == 404
    assert (
        await client.post(f"/api/stories/{v}/merge", json={"source_story_id": e})
    ).status_code == 404
    assert (
        await client.post(f"/api/stories/{e}/merge", json={"source_story_id": v})
    ).status_code == 404
    assert (
        await client.post(f"/api/stories/articles/{a_v}/move", json={"story_id": e})
    ).status_code == 404

    async with db_session() as s:
        assert await s.get(Story, v) is not None and await s.get(Story, e) is not None
        art = await s.get(Article, a_v)
        assert art is not None and art.story_id == v

    await _login(client, ADMIN)
    assert (await client.post(f"/api/stories/{v}/readeck")).status_code == 200
    assert (await client.post(f"/api/stories/{v}/share", json={})).status_code == 200
    assert (await client.post(f"/api/stories/{v}/read")).status_code == 204
    assert (await client.get(f"/api/stories/{v}/diff?from=1")).status_code == 200
    assert (await client.get(f"/api/stories/{v}/similar")).status_code == 200


async def test_move_guards_invisible_article_and_target(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    # Admin may not move a visible article into a story of an unfollowed feed, nor pull an
    # article out of one.
    assert (
        await client.post(
            f"/api/stories/articles/{ids['a_visible']}/move", json={"story_id": ids["hidden"]}
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/stories/articles/{ids['a_hidden']}/move", json={"story_id": ids["visible"]}
        )
    ).status_code == 404
    # An empty target has no owner yet and stays allowed.
    async with db_session() as s:
        empty = Story(title="Empty", summary="", category="tech", version=1)
        s.add(empty)
        await s.commit()
        empty_id = empty.id
    r = await client.post(
        f"/api/stories/articles/{ids['a_visible']}/move", json={"story_id": empty_id}
    )
    assert r.status_code == 204
    async with db_session() as s:
        art = await s.scalar(select(Article).where(Article.id == ids["a_visible"]))
        assert art is not None and art.story_id == empty_id


def test_parse_feeds_subscribers_tolerates_bad_values() -> None:
    def parse(value: object) -> int:
        item: dict[str, object] = {"feedId": "f", "title": "T", "subscribers": value}
        return feed_directory.parse_feeds({"feeds": [item]}, "feeds")[0]["subscribers"]

    assert parse("abc") == 0
    assert parse(None) == 0
    assert parse("12") == 12
    assert parse(3.7) == 3
