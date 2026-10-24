"""Coverage tests: story translation service (cache, fallbacks, prewarm queue, worker)."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import (
    ActivityEvent,
    Article,
    Feed,
    Story,
    StoryTranslation,
    User,
    UserFeed,
)
from app.services import llm_client, translation


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translation, "_queue", asyncio.Queue())
    monkeypatch.setattr(translation, "_worker_task", None)
    monkeypatch.setattr(settings, "summary_language", "en")


def _llm(
    monkeypatch, response: Any = None, exc: Exception | None = None
) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    async def fake(title: str, summary: str, lang: str):
        calls.append((title, summary, lang))
        if exc:
            raise exc
        return response if response is not None else {
            "title": f"T[{lang}]",
            "summary": f"S[{lang}]",
        }, 5

    monkeypatch.setattr(translation, "_translate_llm", fake)
    return calls


async def _story(db_session, **kw: Any) -> Story:
    async with db_session() as s:
        story = Story(title="Hello", summary="World", version=2, **kw)
        s.add(story)
        await s.commit()
        return story


def test_language_name_known_and_unknown() -> None:
    assert translation.language_name("zz-unknown") == "zz-unknown"
    assert translation.language_name("fr") != "fr"


async def test_translate_llm_budget_and_delegation(monkeypatch) -> None:
    monkeypatch.setattr(translation.llm_budget, "input_budget_chars", lambda system: 10)
    with pytest.raises(llm_client.LLMError, match="context budget"):
        await translation._translate_llm("t", "s" * 100, "French")

    monkeypatch.setattr(translation.llm_budget, "input_budget_chars", lambda system: 10**6)
    seen: list[str] = []

    async def fake_chat(system: str, user: str):
        seen.append(user)
        return {"title": "x", "summary": "y"}, 1

    monkeypatch.setattr(llm_client, "chat_json", fake_chat)
    assert await translation._translate_llm("t", "s", "French") == (
        {"title": "x", "summary": "y"},
        1,
    )
    assert "s" in seen[0]


async def test_translate_story_same_language_is_noop(db_session, monkeypatch) -> None:
    calls = _llm(monkeypatch)
    story = await _story(db_session, language="fr")
    async with db_session() as s:
        assert await translation.translate_story(s, story, "fr") == ("Hello", "World")
        assert await translation.translate_story(s, story, "") == ("Hello", "World")
    assert calls == []


async def test_translate_story_creates_then_updates_cache(db_session, monkeypatch) -> None:
    calls = _llm(monkeypatch, {"title": "  Bonjour ", "summary": " Monde\n"})
    story = await _story(db_session)
    async with db_session() as s:
        assert await translation.translate_story(s, story, "fr") == ("Bonjour", "Monde")
        row = await s.get(StoryTranslation, (story.id, "fr"))
        assert row is not None and row.version == 2 and row.title == "Bonjour"
        ev = await s.scalar(select(ActivityEvent).where(ActivityEvent.action == "story_translated"))
        assert ev is not None

        story.version = 3
        _llm(monkeypatch, {"title": "Salut", "summary": "Tout"})
        assert await translation.translate_story(s, story, "fr") == ("Salut", "Tout")
        s.expire_all()
        row = await s.get(StoryTranslation, (story.id, "fr"))
        assert row is not None and (row.version, row.title) == (3, "Salut")
    assert calls[0][2] == translation.language_name("fr")


@pytest.mark.parametrize(
    "response",
    [
        {"title": "", "summary": "x"},
        {"title": "x", "summary": "  "},
        {"title": 5, "summary": "x"},
        {"title": "x"},
    ],
)
async def test_translate_story_incomplete_falls_back_and_caches_nothing(
    db_session, monkeypatch, response
) -> None:
    _llm(monkeypatch, response)
    story = await _story(db_session)
    async with db_session() as s:
        assert await translation.translate_story(s, story, "de") == ("Hello", "World")
        assert await s.get(StoryTranslation, (story.id, "de")) is None


async def test_translate_story_llm_error_falls_back(db_session, monkeypatch) -> None:
    _llm(monkeypatch, exc=llm_client.LLMError("down"))
    story = await _story(db_session)
    async with db_session() as s:
        assert await translation.translate_story(s, story, "de") == ("Hello", "World")


async def test_get_or_translate_uses_cache_only_for_current_version(
    db_session, monkeypatch
) -> None:
    calls = _llm(monkeypatch)
    story = await _story(db_session)
    async with db_session() as s:
        s.add(
            StoryTranslation(
                story_id=story.id,
                language="es",
                version=2,
                title="Hola",
                summary="Mundo",
                created_at=datetime.now(UTC),
            )
        )
        s.add(
            StoryTranslation(
                story_id=story.id,
                language="it",
                version=1,
                title="old",
                summary="old",
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()
        assert await translation.get_or_translate_story(s, story, "es") == ("Hola", "Mundo")
        assert calls == []
        it = translation.language_name("it")
        assert await translation.get_or_translate_story(s, story, "it") == (f"T[{it}]", f"S[{it}]")
        assert await translation.get_or_translate_story(s, story, "en") == ("Hello", "World")
        assert len(calls) == 1


async def test_batch_get_translations(db_session) -> None:
    a = await _story(db_session)
    async with db_session() as s:
        s.add(
            StoryTranslation(
                story_id=a.id,
                language="fr",
                version=2,
                title="t",
                summary="s",
                created_at=datetime.now(UTC),
            )
        )
        await s.commit()
        assert await translation.batch_get_translations(s, [a.id, 99], "fr") == {
            a.id: ("t", "s", 2)
        }
        assert await translation.batch_get_translations(s, [a.id], "de") == {}
        assert await translation.batch_get_translations(s, [], "fr") == {}
        assert await translation.batch_get_translations(s, [a.id], "") == {}


async def test_prewarm_user_stories_enqueues_recent_subscribed(db_session, monkeypatch) -> None:
    async with db_session() as s:
        user = User(username="u", password_hash="x")
        mine = Feed(url="https://m.test/rss")
        other = Feed(url="https://o.test/rss")
        stories = [Story(title=str(i)) for i in range(3)]
        s.add_all([user, mine, other, *stories])
        await s.flush()
        s.add(UserFeed(user_id=user.id, feed_id=mine.id))
        s.add(Article(feed_id=mine.id, guid="a", url="https://m/1", story_id=stories[0].id))
        s.add(Article(feed_id=mine.id, guid="b", url="https://m/2", story_id=stories[1].id))
        s.add(Article(feed_id=mine.id, guid="c", url="https://m/3", story_id=None))
        s.add(Article(feed_id=other.id, guid="d", url="https://o/1", story_id=stories[2].id))
        await s.commit()
        await translation.prewarm_user_stories(s, user.id, "fr")
        queued = [translation._queue.get_nowait() for _ in range(translation._queue.qsize())]
        assert queued == [(stories[1].id, ["fr"]), (stories[0].id, ["fr"])]

        # empty target falls back to the server language; limit applies
        await translation.prewarm_user_stories(s, user.id, "", limit=1)
        assert translation._queue.get_nowait() == (stories[1].id, ["en"])
        # no subscriptions / no language -> nothing queued
        await translation.prewarm_user_stories(s, user.id + 50, "fr")
        monkeypatch.setattr(settings, "summary_language", "")
        await translation.prewarm_user_stories(s, user.id, "")
        assert translation._queue.empty()


async def test_worker_translates_explicit_and_derived_languages(db_session, monkeypatch) -> None:
    calls = _llm(monkeypatch)
    async with db_session() as s:
        fr = User(username="fr", password_hash="x", summary_language="fr")
        en = User(username="en", password_hash="x", summary_language="")
        feed = Feed(url="https://w.test/rss")
        fresh = Story(title="fresh", summary="s", version=1)
        cached = Story(title="cached", summary="s", version=1)
        s.add_all([fr, en, feed, fresh, cached])
        await s.flush()
        s.add_all(
            [
                UserFeed(user_id=fr.id, feed_id=feed.id),
                UserFeed(user_id=en.id, feed_id=feed.id),
                Article(feed_id=feed.id, guid="1", url="https://w/1", story_id=fresh.id),
                Article(feed_id=feed.id, guid="2", url="https://w/2", story_id=cached.id),
                StoryTranslation(
                    story_id=cached.id,
                    language="fr",
                    version=1,
                    title="c",
                    summary="c",
                    created_at=datetime.now(UTC),
                ),
            ]
        )
        await s.commit()
        fresh_id, cached_id = fresh.id, cached.id

    translation.enqueue_story_translation(9999)  # missing story: skipped quietly
    translation.enqueue_story_translation(fresh_id)  # derived: fr only (en == story lang)
    translation.enqueue_story_translation(cached_id)  # already current -> no LLM call
    translation.enqueue_story_translation(fresh_id, ["de", "en", ""])  # explicit; en filtered
    translation.start_translation_worker()
    first = translation._worker_task
    translation.start_translation_worker()
    assert translation._worker_task is first  # idempotent while running
    await asyncio.wait_for(translation._queue.join(), 10)
    await translation.stop_translation_worker()
    assert translation._worker_task is None

    langs = [c[2] for c in calls]
    assert langs == [translation.language_name("fr"), translation.language_name("de")]
    async with db_session() as s:
        rows = (
            await s.scalars(select(StoryTranslation).where(StoryTranslation.story_id == fresh_id))
        ).all()
        assert {r.language for r in rows} == {"fr", "de"}


async def test_worker_survives_errors(db_session, monkeypatch) -> None:
    story = await _story(db_session, language="en")
    boom_calls: list[int] = []

    async def boom(session, story_obj, lang):
        boom_calls.append(1)
        raise RuntimeError("worker boom")

    monkeypatch.setattr(translation, "translate_story", boom)
    translation.enqueue_story_translation(story.id, ["fr"])
    translation.enqueue_story_translation(story.id, ["de"])
    translation.start_translation_worker()
    await asyncio.wait_for(translation._queue.join(), 10)
    assert len(boom_calls) == 2  # first failure did not kill the loop
    await translation.stop_translation_worker()
    await translation.stop_translation_worker()  # no-op when stopped
