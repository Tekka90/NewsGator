"""Per-user Listen resume position: /api/stories/{id}/listen-position."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from tests.conftest import setup_admin

from app.core.config import settings
from app.core.security import make_session_token
from app.models import Article, Feed, ListenPosition, Story, User, UserFeed
from app.services import listen_text, retention
from app.services.vectorstore import InMemoryVectorStore


def _auth(user_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + make_session_token(user_id)}


async def _setup(db_session):
    async with db_session() as s:
        feed = Feed(url="https://example.com/rss", title="Feed")
        s.add(feed)
        story = Story(title="T", summary="S", category="General")
        other_story = Story(title="O", summary="S", category="General")
        s.add_all([story, other_story])
        await s.flush()
        a1 = Article(feed_id=feed.id, guid="a1", url="https://example.com/1", title="A",
                     story_id=story.id, processing_state="clustered")
        a2 = Article(feed_id=feed.id, guid="a2", url="https://example.com/2", title="B",
                     story_id=other_story.id, processing_state="clustered")
        s.add_all([a1, a2])
        owner = User(username="owner", password_hash="x", is_admin=True)
        member = User(username="member", password_hash="x", is_admin=False)
        outsider = User(username="outsider", password_hash="x", is_admin=False)
        s.add_all([owner, member, outsider])
        await s.flush()
        s.add_all([UserFeed(user_id=owner.id, feed_id=feed.id),
                   UserFeed(user_id=member.id, feed_id=feed.id)])
        await s.commit()
        return story.id, a1.id, a2.id, owner.id, member.id, outsider.id


async def test_requires_auth(client: AsyncClient, db_session) -> None:
    story_id, *_ = await _setup(db_session)
    url = f"/api/stories/{story_id}/listen-position"
    assert (await client.get(url)).status_code == 401
    body = {"step": "summary", "chunk": 0, "elapsed_seconds": 0, "story_version": 1}
    assert (await client.put(url, json=body)).status_code == 401
    assert (await client.delete(url)).status_code == 401


async def test_204_then_upsert_overwrite_and_delete(client: AsyncClient, db_session) -> None:
    story_id, a1, _, owner, *_ = await _setup(db_session)
    url = f"/api/stories/{story_id}/listen-position"
    h = _auth(owner)
    assert (await client.get(url, headers=h)).status_code == 204

    r = await client.put(
        url, headers=h, json={"step": "summary", "chunk": 2, "elapsed_seconds": 30,
            "story_version": 1}
    )
    assert r.status_code == 200
    assert r.json()["story_id"] == story_id
    assert r.json()["article_id"] is None
    assert (r.json()["step"], r.json()["chunk"], r.json()["elapsed_seconds"]) == ("summary", 2, 30)

    r = await client.put(
        url, headers=h,
        json={"step": "article", "article_id": str(a1), "chunk": 5, "elapsed_seconds": 99,
            "story_version": 1, "text_length": 0},
    )
    assert r.status_code == 200
    got = await client.get(url, headers=h)
    assert got.status_code == 200
    assert got.json() == r.json()
    assert got.json()["article_id"] == str(a1)
    assert (got.json()["story_version"], got.json()["text_length"]) == (1, 0)
    assert got.json()["updated_at"]

    # a non-article step clears a previously stored article_id
    r = await client.put(url, headers=h, json={"step": "video", "article_id": str(a1),
                                               "chunk": 0, "elapsed_seconds": 1,
                                                   "story_version": 1})
    assert r.json()["step"] == "video"

    assert (await client.delete(url, headers=h)).status_code == 204
    assert (await client.delete(url, headers=h)).status_code == 204
    assert (await client.get(url, headers=h)).status_code == 204


async def test_per_user_isolation(client: AsyncClient, db_session) -> None:
    story_id, _, _, owner, member, _ = await _setup(db_session)
    url = f"/api/stories/{story_id}/listen-position"
    await client.put(url, headers=_auth(owner),
                     json={"step": "summary", "chunk": 3, "elapsed_seconds": 7, "story_version": 1})
    assert (await client.get(url, headers=_auth(member))).status_code == 204
    await client.put(url, headers=_auth(member),
                     json={"step": "summary", "chunk": 9, "elapsed_seconds": 1, "story_version": 1})
    assert (await client.get(url, headers=_auth(owner))).json()["chunk"] == 3
    await client.delete(url, headers=_auth(member))
    assert (await client.get(url, headers=_auth(owner))).status_code == 200


async def test_invisible_or_missing_story_404(client: AsyncClient, db_session) -> None:
    story_id, _, _, _, _, outsider = await _setup(db_session)
    body = {"step": "summary", "chunk": 0, "elapsed_seconds": 0, "story_version": 1}
    for sid in (story_id, 9999):
        url = f"/api/stories/{sid}/listen-position"
        h = _auth(outsider)
        assert (await client.get(url, headers=h)).status_code == 404
        assert (await client.put(url, headers=h, json=body)).status_code == 404
        assert (await client.delete(url, headers=h)).status_code == 404


async def test_validation(client: AsyncClient, db_session) -> None:
    story_id, a1, a2, owner, *_ = await _setup(db_session)
    url = f"/api/stories/{story_id}/listen-position"
    h = _auth(owner)

    async def put(**kw):
        body = {"chunk": 0, "elapsed_seconds": 0, "story_version": 1, "text_length": 0, **kw}
        return (await client.put(url, headers=h, json=body)).status_code

    assert await put(step="article") == 422  # article_id required
    assert await put(step="article", article_id="abc") == 422
    assert await put(step="article", article_id=str(a2)) == 422  # other story's article
    assert await put(step="article", article_id="999999") == 422
    assert await put(step="bogus") == 422
    assert await put(step="article", article_id=str(a1), text_length=None) == 422
    assert await put(step="summary", chunk=-1) == 422
    assert await put(step="summary", elapsed_seconds=-1) == 422
    assert await put(step="summary", chunk=1_000_001) == 422
    assert await put(step="summary", elapsed_seconds=1_000_001) == 422
    assert await put(step="article", article_id=str(a1)) == 200
    assert (await client.get(url, headers=h)).status_code == 200


async def test_cleanup_on_story_purge_and_user_delete(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    story_id, _, _, owner, member, _ = await _setup(db_session)
    old = datetime.now(UTC) - timedelta(days=settings.retention_days + 10)
    async with db_session() as s:
        st = await s.get(Story, story_id)
        assert st is not None
        st.last_updated_at = old
        for a in (await s.scalars(select(Article).where(Article.story_id == story_id))).all():
            a.fetched_at = old
        s.add_all([ListenPosition(user_id=owner, story_id=story_id, step="summary",
            story_version=1),
                   ListenPosition(user_id=member, story_id=story_id, step="summary",
                       story_version=1)])
        await s.commit()

    r = await client.delete(f"/api/users/{member}")
    assert r.status_code == 204, r.text
    async with db_session() as s:
        assert await s.scalar(select(func.count(ListenPosition.user_id))) == 1

    store = InMemoryVectorStore()
    monkeypatch.setattr(retention, "get_vector_store", lambda session=None: store)
    async with db_session() as s:
        await retention.purge_old_data(s)
        assert await s.get(Story, story_id) is None
        assert await s.scalar(select(func.count(ListenPosition.user_id))) == 0


async def _put_article_pos(client, story_id, a1, owner, **kw):
    body = {"step": "article", "article_id": str(a1), "chunk": 1, "elapsed_seconds": 2,
            "story_version": 1, "text_length": 0, **kw}
    return await client.put(
        f"/api/stories/{story_id}/listen-position", headers=_auth(owner), json=body
    )


async def _rows(db_session) -> int:
    async with db_session() as s:
        return await s.scalar(select(func.count(ListenPosition.user_id))) or 0


async def test_stale_version_put_409_and_bump_invalidates(
    client: AsyncClient, db_session
) -> None:
    story_id, _, _, owner, *_ = await _setup(db_session)
    url = f"/api/stories/{story_id}/listen-position"
    h = _auth(owner)
    body = {"step": "summary", "chunk": 0, "elapsed_seconds": 0, "story_version": 5}
    assert (await client.put(url, headers=h, json=body)).status_code == 409
    assert await _rows(db_session) == 0

    body["story_version"] = 1
    assert (await client.put(url, headers=h, json=body)).status_code == 200
    assert (await client.get(url, headers=h)).status_code == 200

    async with db_session() as s:
        st = await s.get(Story, story_id)
        assert st is not None
        st.version = 2
        await s.commit()
    assert (await client.get(url, headers=h)).status_code == 204
    assert await _rows(db_session) == 0


async def test_article_text_change_or_removal_invalidates(
    client: AsyncClient, db_session
) -> None:
    story_id, a1, _, owner, *_ = await _setup(db_session)
    url = f"/api/stories/{story_id}/listen-position"
    h = _auth(owner)

    assert (await _put_article_pos(client, story_id, a1, owner)).status_code == 200
    assert (await client.get(url, headers=h)).status_code == 200  # unchanged

    async with db_session() as s:
        art = await s.get(Article, a1)
        assert art is not None
        art.full_text = "Different reprocessed text. " * 20
        art.content_status = "full"
        await s.commit()
    assert (await client.get(url, headers=h)).status_code == 204
    assert await _rows(db_session) == 0

    # re-stamped with the new length it is valid again
    async with db_session() as s:
        art = await s.get(Article, a1)
        assert art is not None
        length = listen_text.full_text_info(art)[1]
    assert length > 0
    assert (await _put_article_pos(client, story_id, a1, owner,
        text_length=length)).status_code == 200
    assert (await client.get(url, headers=h)).status_code == 200

    async with db_session() as s:  # article moved out of the story
        art = await s.get(Article, a1)
        assert art is not None
        s.add(Article(feed_id=art.feed_id, guid="a3", url="https://example.com/3",
                      title="C", story_id=story_id, processing_state="clustered"))
        art.story_id = None
        await s.commit()
    assert (await client.get(url, headers=h)).status_code == 204
    assert await _rows(db_session) == 0


async def test_manual_merge_deletes_source_positions(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import stories as stories_api

    monkeypatch.setattr(stories_api, "get_vector_store", lambda session=None: InMemoryVectorStore())
    await setup_admin(client)
    story_id, _, _, owner, *_ = await _setup(db_session)
    async with db_session() as s:
        other = await s.scalar(select(Story).where(Story.title == "O"))
        assert other is not None
        s.add_all([ListenPosition(user_id=owner, story_id=other.id, step="summary",
            story_version=1),
                   ListenPosition(user_id=owner, story_id=story_id, step="summary",
                       story_version=1)])
        await s.commit()
        other_id = other.id
    r = await client.post(f"/api/stories/{story_id}/merge", json={"source_story_id": other_id})
    assert r.status_code == 204, r.text
    async with db_session() as s:
        left = (await s.scalars(select(ListenPosition.story_id))).all()
        assert list(left) == [story_id]


async def test_feed_deletion_removes_orphan_story_positions(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import feeds as feeds_api

    monkeypatch.setattr(feeds_api, "get_vector_store", lambda session=None: InMemoryVectorStore())
    await setup_admin(client)
    story_id, *_ = await _setup(db_session)
    async with db_session() as s:
        feed_id = await s.scalar(select(Feed.id))
        await s.execute(delete(UserFeed))  # last subscriber leaving purges the feed
        s.add(ListenPosition(user_id=1, story_id=story_id, step="summary", story_version=1))
        await s.commit()
    r = await client.delete(f"/api/feeds/{feed_id}")
    assert r.status_code == 204, r.text
    assert await _rows(db_session) == 0
