"""Share feature tests: language list, as-is share (no LLM), translation, errors."""

import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.core.config import settings as env_settings
from app.models import Article, Category, Feed, Story
from app.services import share


@pytest.fixture(autouse=True)
def _default_share_languages(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(env_settings, "share_languages", "en,fr,de,es,it,pt,nl")
    monkeypatch.setattr(env_settings, "summary_language", "en")


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


async def test_share_languages_endpoint(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.get("/api/stories/share-languages")
    assert r.status_code == 200
    body = r.json()
    assert body["summary_language"] == "en"
    codes = [lang["code"] for lang in body["languages"]]
    assert codes == ["en", "fr", "de", "es", "it", "pt", "nl"]
    assert body["languages"][1] == {"code": "fr", "name": "French"}


async def test_share_languages_filtered_to_known_codes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    monkeypatch.setattr(env_settings, "share_languages", "de, xx ,de,")
    r = await client.get("/api/stories/share-languages")
    assert r.json()["languages"] == [{"code": "de", "name": "German"}]


async def test_share_as_is_never_calls_llm(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)

    async def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("LLM must not be called when sharing as-is")

    monkeypatch.setattr(share, "_translate", boom)
    r = await client.post(f"/api/stories/{story_id}/share", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["translated"] is False
    assert body["language"] == "en"
    assert body["latency_ms"] == 0
    assert body["title"] == "Big Event"
    assert "Line one." in body["text"]
    assert "https://news.example.com/one" in body["text"]
    assert "https://other.example.com/two" in body["text"]
    assert body["url"] == "https://news.example.com/one"  # earliest = first


async def test_share_with_translation(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)

    captured: dict[str, object] = {}

    async def fake_translate(title: str, summary: str, target_language: str):
        captured.update(title=title, summary=summary, target_language=target_language)
        return {"title": "Großes Ereignis", "summary": "Zeile eins.\n\nZeile zwei."}, 123

    monkeypatch.setattr(share, "_translate", fake_translate)
    r = await client.post(f"/api/stories/{story_id}/share", json={"language": "de"})
    assert r.status_code == 200
    body = r.json()
    assert captured["target_language"] == "German"
    assert body["translated"] is True
    assert body["language"] == "de"
    assert body["latency_ms"] == 123
    assert body["title"] == "Großes Ereignis"
    assert "Zeile eins." in body["text"]
    # source links are never translated
    assert "https://news.example.com/one" in body["text"]


async def test_share_same_language_as_summary_skips_translation(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)

    async def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("no translation needed for the summary language")

    monkeypatch.setattr(share, "_translate", boom)
    r = await client.post(f"/api/stories/{story_id}/share", json={"language": "en"})
    assert r.status_code == 200
    assert r.json()["translated"] is False


async def test_share_unknown_language_rejected(
    client: AsyncClient, db_session
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)
    r = await client.post(f"/api/stories/{story_id}/share", json={"language": "xx"})
    assert r.status_code == 400


async def test_share_unknown_story_404(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post("/api/stories/999/share", json={})
    assert r.status_code == 404


async def test_share_incomplete_translation_is_400(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)

    async def bad_translate(title: str, summary: str, target_language: str):
        return {"title": "", "summary": "ok"}, 10

    monkeypatch.setattr(share, "_translate", bad_translate)
    r = await client.post(f"/api/stories/{story_id}/share", json={"language": "fr"})
    assert r.status_code == 400


async def test_share_emits_activity_events(
    client: AsyncClient, db_session
) -> None:
    story_id = await _make_story(db_session)
    await setup_admin(client)
    r = await client.post(f"/api/stories/{story_id}/share", json={})
    assert r.status_code == 200
    events = await client.get("/api/activity/recent?component=share")
    assert events.status_code == 200
    actions = [e["action"] for e in events.json()["events"]]
    assert "prepare_start" in actions
    assert "prepare_done" in actions
