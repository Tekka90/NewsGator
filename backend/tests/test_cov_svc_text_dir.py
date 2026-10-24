"""Coverage tests: listen_text article translation and the feed directory client."""

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.models import ActivityEvent, Article, ArticleTranslation, Feed
from app.services import feed_directory, listen_text, llm_client

# --- listen_text.translate_article_text ---


async def _article(db_session) -> Article:
    async with db_session() as s:
        feed = Feed(url="https://l.test/rss")
        s.add(feed)
        await s.flush()
        art = Article(feed_id=feed.id, guid="1", url="https://l.test/1", title="t")
        s.add(art)
        await s.commit()
        return art


PARAS = ["Alpha " * 24, "Bravo " * 24, "Charlie " * 24]
TEXT = "\n\n".join(p.strip() for p in PARAS)


def test_hash_is_stable_and_content_sensitive() -> None:
    assert listen_text._hash("a") == listen_text._hash("a")
    assert listen_text._hash("a") != listen_text._hash("b")


def test_full_text_info_counts_cleaned_chars() -> None:
    art = Article(content_status="ok", full_text=None)
    assert listen_text.full_text_info(art) == (False, 0)


async def test_translate_chunk_seam_calls_chat_text(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    async def fake(system: str, user: str):
        seen.append((system, user))
        return "traduit", 4

    monkeypatch.setattr(llm_client, "chat_text", fake)
    assert await listen_text._translate_chunk("hello", "French") == ("traduit", 4)
    assert "hello" in seen[0][1] and "French" in (seen[0][0] + seen[0][1])


async def test_translate_article_chunks_joins_and_caches(db_session, monkeypatch) -> None:
    art = await _article(db_session)
    # budget forces one paragraph per chunk (budget//2 ≈ 200 chars)
    monkeypatch.setattr(listen_text.llm_budget, "input_budget_chars", lambda system: 400)
    chunks: list[str] = []

    async def fake_chunk(chunk: str, target: str):
        chunks.append(chunk)
        return f"  <{target}:{len(chunks)}>  ", 3

    monkeypatch.setattr(listen_text, "_translate_chunk", fake_chunk)
    async with db_session() as s:
        out = await listen_text.translate_article_text(s, art, TEXT, "fr")
        assert len(chunks) == 3 and all(len(c) <= 200 for c in chunks)
        assert out == "\n\n".join(
            f"<{listen_text.prompts.LANGUAGE_NAMES.get('fr', 'fr')}:{i}>" for i in (1, 2, 3)
        )
        row = await s.get(ArticleTranslation, (art.id, "fr"))
        assert row is not None and row.text == out and row.source_hash == listen_text._hash(TEXT)
        actions = [
            e.action for e in await s.scalars(select(ActivityEvent).order_by(ActivityEvent.id))
        ]
        assert actions == ["article_translate_start", "article_translated"]

        # cache hit: no further LLM calls
        assert await listen_text.translate_article_text(s, art, TEXT, "fr") == out
        assert len(chunks) == 3

        # changed source refreshes the cached row in place
        async def one(chunk: str, target: str):
            return "nouveau", 1

        monkeypatch.setattr(listen_text, "_translate_chunk", one)
        monkeypatch.setattr(listen_text.llm_budget, "input_budget_chars", lambda system: 10**6)
        assert await listen_text.translate_article_text(s, art, "different text", "fr") == "nouveau"
        s.expire_all()
        row = await s.get(ArticleTranslation, (art.id, "fr"))
        assert (
            row is not None
            and row.text == "nouveau"
            and row.source_hash == listen_text._hash("different text")
        )


async def test_translate_article_too_long_and_failures(db_session, monkeypatch) -> None:
    art = await _article(db_session)
    monkeypatch.setattr(listen_text.llm_budget, "input_budget_chars", lambda system: 400)
    monkeypatch.setattr(listen_text.llm_budget, "MAX_CALLS_PER_ARTICLE", 2)
    async with db_session() as s:
        with pytest.raises(listen_text.TranslationTooLongError, match="3 translation calls"):
            await listen_text.translate_article_text(s, art, TEXT, "fr")
        assert await s.scalar(select(ActivityEvent)) is None  # rejected before any event

    monkeypatch.setattr(listen_text.llm_budget, "MAX_CALLS_PER_ARTICLE", 16)

    async def blank(chunk: str, target: str):
        return "   ", 1

    monkeypatch.setattr(listen_text, "_translate_chunk", blank)
    async with db_session() as s:
        with pytest.raises(llm_client.LLMError, match="empty translation"):
            await listen_text.translate_article_text(s, art, TEXT, "xx-unknown")
        err = await s.scalar(
            select(ActivityEvent).where(ActivityEvent.action == "article_translate_error")
        )
        assert err is not None and err.level == "error" and "empty translation" in err.detail
        assert await s.get(ArticleTranslation, (art.id, "xx-unknown")) is None


# --- feed_directory ---


@pytest.fixture(autouse=True)
def _fast_directory(monkeypatch) -> list[float]:
    feed_directory.clear_cache()
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(feed_directory.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(feed_directory, "_next_slot", 0.0)
    yield sleeps
    feed_directory.clear_cache()


def _resp(status: int, payload: Any = None, bad_json: bool = False) -> httpx.Response:
    if bad_json:
        return httpx.Response(status, content=b"<html>", request=httpx.Request("GET", "http://x"))
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


def test_focus_topics() -> None:
    assert feed_directory.focus_topics("   ") == []
    assert feed_directory.focus_topics("Climate") == ["climate"]
    assert feed_directory.focus_topics("Deep  Sea a Fishing Boats") == [
        "deep sea a fishing boats",
        "deep",
        "sea",
        "fishing",
    ]


def test_parse_feeds_normalizes_and_skips_invalid() -> None:
    data = {
        "k": [
            {
                "feedId": "feed/http://a",
                "title": " A ",
                "website": "http://a",
                "subscribers": "12",
                "language": "en",
                "iconUrl": "i.png",
                "description": None,
            },
            {"feedId": "feed/http://b", "visualUrl": "v.png", "website": 5, "language": 3},
            {"title": "no id"},
            "junk",
        ]
    }
    feeds = feed_directory.parse_feeds(data, "k")
    assert feeds[0] == {
        "feed_id": "feed/http://a",
        "title": "A",
        "website": "http://a",
        "description": "",
        "subscribers": 12,
        "language": "en",
        "icon_url": "i.png",
    }
    assert (
        feeds[1]["icon_url"] == "v.png"
        and feeds[1]["website"] is None
        and feeds[1]["language"] is None
    )
    assert len(feeds) == 2
    assert feed_directory.parse_feeds([], "k") == []
    assert feed_directory.parse_feeds({"k": "nope"}, "k") == []


async def test_topic_feeds_fetches_once_then_caches(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []

    async def fake_get(url: str, params=None):
        calls.append((url, params))
        return _resp(200, {"feedInfos": [{"feedId": "feed/x", "title": "X"}]})

    monkeypatch.setattr(feed_directory, "_http_get", fake_get)
    first = await feed_directory.topic_feeds(" Sci Fi ", "fr")
    second = await feed_directory.topic_feeds("Sci Fi", "fr")
    assert first == second and first[0]["feed_id"] == "feed/x"
    assert calls == [
        (f"{feed_directory.BASE_URL}/recommendations/topics/Sci%20Fi", {"locale": "fr"})
    ]
    assert await feed_directory.topic_feeds("  ", "fr") == []
    await feed_directory.topic_feeds("news", None)
    assert calls[-1][1] is None


async def test_search_feeds_params_and_blank(monkeypatch) -> None:
    calls: list[tuple[str, Any]] = []

    async def fake_get(url: str, params=None):
        calls.append((url, params))
        return _resp(200, {"results": [{"feedId": "feed/s", "title": "S"}]})

    monkeypatch.setattr(feed_directory, "_http_get", fake_get)
    assert await feed_directory.search_feeds("  ", "en", 5) == []
    out = await feed_directory.search_feeds(" rust ", "de", 7)
    assert out[0]["title"] == "S"
    assert calls == [
        (f"{feed_directory.BASE_URL}/search/feeds", {"query": "rust", "count": 7, "locale": "de"})
    ]
    await feed_directory.search_feeds("go", None, 3)
    assert "locale" not in calls[-1][1]


async def test_fetch_retries_once_on_429(monkeypatch, _fast_directory) -> None:
    responses = [_resp(429), _resp(200, {"feedInfos": [{"feedId": "feed/r"}]})]

    async def fake_get(url: str, params=None):
        return responses.pop(0)

    monkeypatch.setattr(feed_directory, "_http_get", fake_get)
    out = await feed_directory.topic_feeds("t", None)
    assert [f["feed_id"] for f in out] == ["feed/r"]
    assert feed_directory.BACKOFF_S in _fast_directory

    feed_directory.clear_cache()
    responses[:] = [_resp(429), _resp(429)]
    assert await feed_directory.topic_feeds("t", None) == []


async def test_fetch_error_handling_and_negative_cache(monkeypatch) -> None:
    attempts = {"n": 0}

    async def not_found(url: str, params=None):
        attempts["n"] += 1
        return _resp(404)

    monkeypatch.setattr(feed_directory, "_http_get", not_found)
    assert await feed_directory.topic_feeds("nope", None) == []
    assert await feed_directory.topic_feeds("nope", None) == []
    assert attempts["n"] == 1  # 4xx is remembered

    async def server_error(url: str, params=None):
        attempts["n"] += 1
        return _resp(503)

    monkeypatch.setattr(feed_directory, "_http_get", server_error)
    assert await feed_directory.topic_feeds("flaky", None) == []
    assert await feed_directory.topic_feeds("flaky", None) == []
    assert attempts["n"] == 3  # 5xx not cached

    async def boom(url: str, params=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(feed_directory, "_http_get", boom)
    assert await feed_directory.topic_feeds("offline", None) == []

    async def bad_json(url: str, params=None):
        return _resp(200, bad_json=True)

    monkeypatch.setattr(feed_directory, "_http_get", bad_json)
    assert await feed_directory.topic_feeds("garbled", None) == []


async def test_wait_for_slot_spaces_requests(monkeypatch, _fast_directory) -> None:
    clock = {"t": 100.0}
    monkeypatch.setattr(feed_directory.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(feed_directory, "_next_slot", 0.0)
    await feed_directory._wait_for_slot()  # first one is immediate
    assert _fast_directory == []
    await feed_directory._wait_for_slot()  # same instant: wait one spacing
    assert _fast_directory == [pytest.approx(feed_directory.SPACING_S)]


async def test_http_get_sends_user_agent(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *a: Any) -> None: ...

        async def get(self, url: str, params=None) -> str:
            seen["url"], seen["params"] = url, params
            return "resp"

    monkeypatch.setattr(feed_directory.httpx, "AsyncClient", FakeClient)
    assert await feed_directory._http_get("http://d", {"a": 1}) == "resp"
    assert seen["headers"]["User-Agent"] == feed_directory.USER_AGENT
    assert seen["follow_redirects"] is True and seen["params"] == {"a": 1}
