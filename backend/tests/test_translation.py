"""Tests for multi-language story translation service and caching."""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.core.config import settings as env_settings
from app.core.security import make_session_token
from app.models import Article, Category, Feed, Story, StoryTranslation, User, UserFeed
from app.services import translation


@pytest.fixture(autouse=True)
def _set_summary_lang(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(env_settings, "summary_language", "en")


async def _setup_story(db_session, lang: str = "de") -> tuple[int, int, int]:
    """Create a feed, an article, a story, and a user with the given summary_language."""
    async with db_session() as s:
        cat = Category(name="General")
        s.add(cat)
        feed = Feed(url="https://example.com/rss", title="News Feed")
        s.add(feed)
        await s.flush()

        story = Story(
            title="Apple announces new product",
            summary="Apple has announced a brand new product today.",
            category="General",
            version=1,
        )
        s.add(story)
        await s.flush()

        article = Article(
            feed_id=feed.id,
            guid="guid-1",
            url="https://example.com/article-1",
            title="Article 1",
            story_id=story.id,
            processing_state="clustered",
        )
        s.add(article)

        user = User(
            username="german_user",
            password_hash="hash",
            is_admin=False,
            summary_language=lang,
        )
        s.add(user)
        await s.flush()

        # Subscribe user to feed
        s.add(UserFeed(user_id=user.id, feed_id=feed.id))
        await s.commit()

        return feed.id, story.id, user.id


async def test_translate_story_caches_and_returns(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, story_id, _ = await _setup_story(db_session)

    mock_llm = AsyncMock(
        return_value=(
            {
                "title": "Apple kündigt neues Produkt an",
                "summary": "Apple hat heute ein brandneues Produkt angekündigt.",
            },
            150,
        )
    )
    monkeypatch.setattr(translation, "_translate_llm", mock_llm)

    async with db_session() as session:
        story = await session.get(Story, story_id)
        assert story is not None

        title, summary = await translation.translate_story(session, story, "de")
        assert title == "Apple kündigt neues Produkt an"
        assert summary == "Apple hat heute ein brandneues Produkt angekündigt."

        # Verify cached in DB
        trans = await session.get(StoryTranslation, (story_id, "de"))
        assert trans is not None
        assert trans.version == 1
        assert trans.title == title
        assert trans.summary == summary


async def test_get_or_translate_story_reuses_cache(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, story_id, _ = await _setup_story(db_session)

    mock_llm = AsyncMock(
        return_value=(
            {"title": "Deutscher Titel", "summary": "Deutsche Zusammenfassung."},
            120,
        )
    )
    monkeypatch.setattr(translation, "_translate_llm", mock_llm)

    async with db_session() as session:
        story = await session.get(Story, story_id)
        assert story is not None

        # First call triggers LLM
        t1, s1 = await translation.get_or_translate_story(session, story, "de")
        assert mock_llm.call_count == 1
        assert t1 == "Deutscher Titel"

        # Second call uses cache (no new LLM call)
        t2, s2 = await translation.get_or_translate_story(session, story, "de")
        assert mock_llm.call_count == 1
        assert t2 == t1
        assert s2 == s1

        # Story version bump invalidates cache
        story.version = 2
        await session.commit()

        mock_llm.return_value = (
            {"title": "Neuer deutscher Titel v2", "summary": "Neue Zusammenfassung v2."},
            100,
        )
        t3, _ = await translation.get_or_translate_story(session, story, "de")
        assert mock_llm.call_count == 2
        assert t3 == "Neuer deutscher Titel v2"


async def test_api_list_stories_returns_translated_for_user(
    client: AsyncClient, db_session
) -> None:
    _, story_id, user_id = await _setup_story(db_session, lang="de")

    # Pre-seed translation
    async with db_session() as session:
        session.add(
            StoryTranslation(
                story_id=story_id,
                language="de",
                version=1,
                title="Apple DE Titel",
                summary="Apple DE Zusammenfassung",
            )
        )
        await session.commit()

    # Login as german user
    from app.core.security import make_session_token

    token = make_session_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    res = await client.get("/api/stories", headers=headers)
    assert res.status_code == 200
    stories = res.json()
    assert len(stories) == 1
    assert stories[0]["id"] == story_id
    assert stories[0]["title"] == "Apple DE Titel"
    assert stories[0]["summary"] == "Apple DE Zusammenfassung"


async def test_api_story_detail_translates_jit(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, story_id, user_id = await _setup_story(db_session, lang="de")

    mock_llm = AsyncMock(
        return_value=(
            {"title": "JIT Titel", "summary": "JIT Zusammenfassung"},
            110,
        )
    )
    monkeypatch.setattr(translation, "_translate_llm", mock_llm)

    token = make_session_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    res = await client.get(f"/api/stories/{story_id}", headers=headers)
    assert res.status_code == 200
    detail = res.json()
    assert detail["title"] == "JIT Titel"
    assert detail["summary"] == "JIT Zusammenfassung"


async def test_patch_me_triggers_prewarm(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, user_id = await _setup_story(db_session, lang="")

    mock_prewarm = AsyncMock()
    monkeypatch.setattr(translation, "prewarm_user_stories", mock_prewarm)

    token = make_session_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    res = await client.patch(
        "/api/auth/me", json={"summary_language": "de"}, headers=headers
    )
    assert res.status_code == 200
    assert res.json()["summary_language"] == "de"
    assert mock_prewarm.call_count == 1
    call_args = mock_prewarm.call_args[0]
    assert call_args[1] == user_id
    assert call_args[2] == "de"

