"""Pipeline edge cases: clustering LLM failures, map-reduce condensation, ingest helpers."""

from datetime import UTC, datetime

import feedparser
import httpx
import numpy as np
import pytest
from sqlalchemy import select
from tests.test_cluster import _embedded_article, _mock_llm, _vec_with_sim, unit_vec

from app.core.config import settings
from app.models import Article, Feed, Story
from app.services import cluster, condense, ingest, llm_budget, llm_client
from app.services.vectorstore import InMemoryVectorStore

# ---------- cluster ----------


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    s = InMemoryVectorStore()
    monkeypatch.setattr(cluster, "get_vector_store", lambda session=None: s)
    return s


async def test_cluster_ignores_missing_or_unembedded_article(db_session, store) -> None:
    async with db_session() as s:
        await cluster.cluster_article(s, 999999)
        assert await cluster.decisions_count(s) == 0
    article = await _embedded_article(db_session, "Something.")
    async with db_session() as s:
        a = await s.get(Article, article.id)
        assert a is not None
        a.processing_state = "summarized"
        await s.commit()
        await cluster.cluster_article(s, article.id)
        assert (await s.get(Article, article.id)).story_id is None  # type: ignore[union-attr]


async def test_story_without_centroid_is_not_a_candidate(
    db_session, store, monkeypatch
) -> None:
    _mock_llm(monkeypatch, default_vec=unit_vec(3))
    a1 = await _embedded_article(db_session, "Event.")
    a2 = await _embedded_article(db_session, "Event again.")
    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)

        async def none(_story_id: int) -> None:
            return None

        monkeypatch.setattr(store, "get_story_centroid", none)
        await cluster.cluster_article(s, a2.id)
        assert len((await s.scalars(select(Story))).all()) == 2
        assert await cluster.decisions_count(s) == 2


async def test_gray_zone_llm_error_creates_new_story(db_session, store, monkeypatch) -> None:
    monkeypatch.setattr(settings, "tau_attach", 0.9)
    monkeypatch.setattr(settings, "tau_gray", 0.5)
    _mock_llm(
        monkeypatch,
        vectors={
            "An article\n\nG one.": unit_vec(20),
            "An article\n\nG two.": _vec_with_sim(20, 0.75),
        },
    )
    a1 = await _embedded_article(db_session, "G one.")
    a2 = await _embedded_article(db_session, "G two.")
    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)

        async def boom(system: str, user: str, model: str | None = None):
            raise llm_client.LLMError("down")

        monkeypatch.setattr(cluster.llm_client, "chat_json", boom)
        await cluster.cluster_article(s, a2.id)
        assert len((await s.scalars(select(Story))).all()) == 2


async def test_llm_failures_degrade_gracefully(db_session, store, monkeypatch) -> None:
    """Headline failure → article title; novelty/merge failures → still versioned."""
    monkeypatch.setattr(settings, "tau_attach", 0.8)
    _mock_llm(monkeypatch, default_vec=unit_vec(4))

    async def boom(system: str, user: str, model: str | None = None):
        raise llm_client.LLMError("down")

    monkeypatch.setattr(cluster.llm_client, "chat_json", boom)
    a1 = await _embedded_article(db_session, "First.", title="Original title")
    a2 = await _embedded_article(db_session, "Second.")
    async with db_session() as s:
        await cluster.cluster_article(s, a1.id)
        story_id = (await s.get(Article, a1.id)).story_id  # type: ignore[union-attr]
        story = await s.get(Story, story_id)
        assert story is not None and story.title == "Original title"
        old_summary = story.summary

        await cluster.cluster_article(s, a2.id)
        await s.refresh(story)
        assert (await s.get(Article, a2.id)).story_id == story_id  # type: ignore[union-attr]
        assert story.version == 2  # novelty check failed → assume new facts
        assert story.summary == old_summary and story.title == "Original title"


async def test_centroid_seeded_when_story_has_none(db_session, monkeypatch) -> None:
    store = InMemoryVectorStore()
    monkeypatch.setattr(cluster, "get_vector_store", lambda session=None: store)
    async with db_session() as s:
        story = Story(title="t", summary="s", category="x")
        s.add(story)
        await s.flush()
        vec = np.asarray(unit_vec(5), dtype=np.float32)
        await cluster._update_centroid(s, story, vec)
        assert store.centroids[story.id] == pytest.approx(vec.tolist())


# ---------- condense ----------


async def _article(db_session) -> Article:
    async with db_session() as s:
        feed = Feed(url="https://c.example.com/rss")
        s.add(feed)
        await s.flush()
        art = Article(feed_id=feed.id, guid="c", url="https://c.example.com/1", title="Long")
        s.add(art)
        await s.commit()
        return art


async def test_fit_text_passthrough_when_it_fits(db_session) -> None:
    art = await _article(db_session)
    async with db_session() as s:
        assert await condense.fit_text(s, art, "short text", "sys", "") == "short text"


async def test_fit_text_second_pass_then_even_sampling(db_session, monkeypatch) -> None:
    art = await _article(db_session)
    monkeypatch.setattr(llm_budget, "input_budget_chars", lambda system, fixed="": 1000)
    calls: list[str] = []

    async def chat(system: str, user: str, model: str | None = None):
        calls.append(user)
        return {"digest": "d" * 800}, 5

    monkeypatch.setattr(condense.llm_client, "chat_json", chat)
    text = "\n\n".join(f"Paragraph {i}. " + "w " * 200 for i in range(12))
    async with db_session() as s:
        out = await condense.fit_text(s, art, text, "sys", "")
    assert 0 < len(out) <= 1000
    assert len(calls) <= llm_budget.MAX_CALLS_PER_ARTICLE - 1


async def test_fit_text_chunk_failure_raises_condense_error(db_session, monkeypatch) -> None:
    art = await _article(db_session)
    monkeypatch.setattr(llm_budget, "input_budget_chars", lambda system, fixed="": 1000)

    async def boom(system: str, user: str, model: str | None = None):
        raise llm_client.LLMError("down")

    monkeypatch.setattr(condense.llm_client, "chat_json", boom)
    async with db_session() as s:
        with pytest.raises(condense.CondenseError):
            await condense.fit_text(s, art, "x " * 3000, "sys", "")


# ---------- ingest ----------


def test_render_opml_skips_mail_feeds() -> None:
    rss = Feed(url="https://a.example.com/rss", title="A", kind="rss")
    mail = Feed(url="newsletter:a@b.c", title="N", kind="mail")
    out = ingest.render_opml([rss, mail])
    assert "https://a.example.com/rss" in out and "newsletter:" not in out


async def test_http_get_sends_conditional_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=b"<rss/>", headers={"etag": "e2"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        ingest.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    feed = Feed(url="https://x.example.com/rss", etag="e1", last_modified="Mon, 01 Jan 2024")
    status, body, headers = await ingest._http_get(feed)
    assert (status, body, headers["etag"]) == (200, b"<rss/>", "e2")
    assert seen["if-none-match"] == "e1"
    assert seen["if-modified-since"] == "Mon, 01 Jan 2024"


def test_entry_published_falls_back_to_parsed_struct_for_iso_dates() -> None:
    parsed = feedparser.parse(
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id>'
        b"<updated>2024-03-05T10:00:00Z</updated></entry></feed>"
    )
    assert ingest._entry_published(parsed.entries[0]) == datetime(2024, 3, 5, 10, tzinfo=UTC)


def test_entry_image_from_enclosure_link_and_protocol_relative_inline() -> None:
    entry = feedparser.FeedParserDict(
        {
            "links": [
                {"rel": "enclosure", "href": "https://i.example.com/a.jpg", "type": "image/jpeg"}
            ]
        }
    )
    assert ingest._entry_image(entry) == "https://i.example.com/a.jpg"
    assert (
        ingest.inline_image_from_html('<img src="//cdn.example.com/p.jpg">')
        == "https://cdn.example.com/p.jpg"
    )


async def test_poll_http_error_records_failure_and_skips_incomplete_entries(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session() as s:
        feed = Feed(url="https://err.example.com/rss", fetch_fulltext=False)
        s.add(feed)
        await s.commit()
        fid = feed.id

    async def server_error(feed: Feed) -> tuple[int, bytes, dict[str, str]]:
        return 500, b"", {}

    monkeypatch.setattr(ingest, "_http_get", server_error)
    async with db_session() as s:
        f = await s.get(Feed, fid)
        assert f is not None
        assert await ingest.poll_feed(s, f) == 0
        assert f.consecutive_failures == 1 and "HTTP 500" in (f.last_error or "")

    rss = (
        b"<rss version='2.0'><channel><title>T</title>"
        b"<item><title>No link</title></item></channel></rss>"
    )

    async def no_link(feed: Feed) -> tuple[int, bytes, dict[str, str]]:
        return 200, rss, {}

    monkeypatch.setattr(ingest, "_http_get", no_link)
    async with db_session() as s:
        f = await s.get(Feed, fid)
        assert f is not None
        assert await ingest.poll_feed(s, f) == 0
        assert f.consecutive_failures == 0
