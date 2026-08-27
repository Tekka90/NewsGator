"""Readeck integration tests: enabled gate, endpoint, rendering, settings."""

import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.core.config import settings as env_settings
from app.models import Article, Category, Feed, Story
from app.services import readeck


@pytest.fixture(autouse=True)
def _configure_readeck(monkeypatch: pytest.MonkeyPatch):
    """Readeck disabled by default; restore config after each test."""
    monkeypatch.setattr(env_settings, "readeck_base_url", None)
    monkeypatch.setattr(env_settings, "readeck_token", None)


async def _make_story(db_session) -> int:
    async with db_session() as s:
        s.add(Category(name="tech"))
        feed = Feed(url="https://f.example.com/rss", title="Feed")
        s.add(feed)
        await s.flush()
        story = Story(
            title="Big Event",
            summary="Line one.\n\nLine two.",
            category="tech",
            image_url="https://img.example.com/lead.jpg",
        )
        s.add(story)
        await s.flush()
        s.add(
            Article(
                feed_id=feed.id,
                guid="a1",
                url="https://news.example.com/one",
                title="Article One",
                story_id=story.id,
                processing_state="clustered",
            )
        )
        s.add(
            Article(
                feed_id=feed.id,
                guid="a2",
                url="https://other.example.com/two",
                title="Article Two",
                story_id=story.id,
                processing_state="clustered",
            )
        )
        await s.commit()
        return story.id


async def test_disabled_by_default(client: AsyncClient, db_session) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)
    assert readeck.is_enabled() is False
    r = await client.post(f"/api/stories/{story_id}/readeck")
    assert r.status_code == 404


async def test_save_posts_multipart_and_returns_bookmark(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)
    monkeypatch.setattr(env_settings, "readeck_base_url", "http://readeck.test")
    monkeypatch.setattr(env_settings, "readeck_token", "tok")
    assert readeck.is_enabled() is True

    captured: dict[str, object] = {}

    async def fake_post(*, url, title, html, labels, created):
        captured.update(url=url, title=title, html=html, labels=labels, created=created)
        return {"bookmark_id": "abc123", "href": "http://readeck.test/api/bookmarks/abc123"}

    monkeypatch.setattr(readeck, "_post_bookmark", fake_post)

    r = await client.post(f"/api/stories/{story_id}/readeck")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bookmark_id"] == "abc123"
    assert body["href"].endswith("/api/bookmarks/abc123")

    # canonical url = earliest source article, title = headline, labels include category
    assert captured["url"] == "https://news.example.com/one"
    assert captured["title"] == "Big Event"
    assert captured["labels"] == ["newsgator", "tech"]
    html = captured["html"]
    assert "<h1>Big Event</h1>" in html
    assert "<p>Line one.</p><p>Line two.</p>" in html
    assert 'href="https://news.example.com/one"' in html
    assert 'href="https://other.example.com/two"' in html
    assert "Article Two" in html
    assert 'src="https://img.example.com/lead.jpg"' in html

    # bookmark id persisted on the story and surfaced in the API
    r = await client.get(f"/api/stories/{story_id}")
    assert r.json()["readeck_bookmark_id"] == "abc123"
    r = await client.get("/api/stories?filter=all")
    item = next(s for s in r.json() if s["id"] == story_id)
    assert item["readeck_bookmark_id"] == "abc123"
    async with db_session() as s:
        story = await s.get(Story, story_id)
        assert story is not None and story.readeck_bookmark_id == "abc123"

    # activity events emitted (invariant 6)
    async with db_session() as s:
        from sqlalchemy import select

        from app.models import ActivityEvent

        rows = (
            await s.scalars(
                select(ActivityEvent).where(ActivityEvent.component == "readeck")
            )
        ).all()
        assert [r.action for r in rows] == ["save_start", "save_done"]


async def test_save_failure_is_502_and_logged(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)
    monkeypatch.setattr(env_settings, "readeck_base_url", "http://readeck.test")
    monkeypatch.setattr(env_settings, "readeck_token", "tok")

    async def boom(**kwargs):
        raise readeck.ReadeckError("Readeck request failed: boom")

    monkeypatch.setattr(readeck, "_post_bookmark", boom)
    r = await client.post(f"/api/stories/{story_id}/readeck")
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]

    async with db_session() as s:
        from sqlalchemy import select

        from app.models import ActivityEvent

        rows = (
            await s.scalars(
                select(ActivityEvent).where(ActivityEvent.component == "readeck")
            )
        ).all()
        assert [r.action for r in rows] == ["save_start", "save_failed"]
        assert rows[-1].level == "error"


async def test_story_not_found(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    await setup_admin(client)
    monkeypatch.setattr(env_settings, "readeck_base_url", "http://readeck.test")
    monkeypatch.setattr(env_settings, "readeck_token", "tok")
    assert (await client.post("/api/stories/999/readeck")).status_code == 404


async def test_settings_whitelist(client: AsyncClient) -> None:
    """Readeck keys are runtime-overridable via the settings API."""
    await setup_admin(client)
    r = await client.get("/api/settings")
    assert "readeck_base_url" in r.json()["values"]
    assert "readeck_token" in r.json()["values"]

    r = await client.patch(
        "/api/settings",
        json={"values": {"readeck_base_url": "http://readeck.test", "readeck_token": "tok"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["values"]["readeck_base_url"] == "http://readeck.test"
    assert "readeck_base_url" in r.json()["overridden"]
    assert env_settings.readeck_base_url == "http://readeck.test"
    assert readeck.is_enabled() is True


async def test_env_locked_not_overridable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env-set Readeck keys are env_locked and reject DB overrides (invariant 5)."""
    await setup_admin(client)
    monkeypatch.setenv("READECK_BASE_URL", "http://env.test")
    r = await client.get("/api/settings")
    assert "readeck_base_url" in r.json()["env_locked"]
    assert r.json()["values"]["readeck_base_url"] == "http://env.test"
    r = await client.patch(
        "/api/settings", json={"values": {"readeck_base_url": "http://other.test"}}
    )
    assert r.status_code == 400
    assert "environment variable" in r.json()["detail"]


def test_empty_str_env_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose passing empty ${VAR} must not count as configured."""
    monkeypatch.setenv("READECK_BASE_URL", "")
    monkeypatch.setenv("READECK_TOKEN", "")
    from app.core.config import Settings

    s = Settings()
    assert s.readeck_base_url is None
    assert s.readeck_token is None
