"""Coverage tests: chat service (retrieval scoping, citations, error branches, history)."""

from typing import Any

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import ActivityEvent, Article, Feed, Story, User, UserFeed
from app.services import chat, llm_client
from app.services.vectorstore import InMemoryVectorStore


@pytest.fixture(autouse=True)
def _chat_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_enabled", True)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    s = InMemoryVectorStore()
    monkeypatch.setattr(chat, "get_vector_store", lambda session=None: s)
    return s


async def _world(db_session, store: InMemoryVectorStore) -> dict[str, int]:
    """Two users; story A (user 1's feed, two hosts), story B (user 2's feed only)."""
    async with db_session() as s:
        u1 = User(username="u1", password_hash="x")
        u2 = User(username="u2", password_hash="x")
        f1 = Feed(url="https://f1.test/rss")
        f2 = Feed(url="https://f2.test/rss")
        a = Story(title="Story A", summary="About A.", category="tech")
        b = Story(title="Story B", summary="About B.", category="sport")
        s.add_all([u1, u2, f1, f2, a, b])
        await s.flush()
        s.add_all(
            [
                UserFeed(user_id=u1.id, feed_id=f1.id),
                UserFeed(user_id=u2.id, feed_id=f2.id),
                Article(feed_id=f1.id, guid="1", url="https://www.one.test/a", story_id=a.id),
                Article(feed_id=f1.id, guid="2", url="https://two.test/a", story_id=a.id),
                Article(feed_id=f1.id, guid="3", url="https://one.test/b", story_id=a.id),
                Article(feed_id=f2.id, guid="4", url="https://three.test/b", story_id=b.id),
            ]
        )
        await s.commit()
        ids = {"u1": u1.id, "u2": u2.id, "a": a.id, "b": b.id}
    await store.upsert_story_centroid(ids["a"], [1.0, 0.0])
    await store.upsert_story_centroid(ids["b"], [0.9, 0.1])
    return ids


def _patch_llm(monkeypatch, *, vectors=None, answer=None, exc: Exception | None = None):
    async def embed(q: str) -> list[list[float]]:
        return [[1.0, 0.0]] if vectors is None else vectors

    async def ans(system: str, user: str) -> tuple[dict[str, Any], int]:
        if exc:
            raise exc
        return answer or {"answer": "ok", "story_ids": []}, 7

    monkeypatch.setattr(chat, "_embed_query", embed)
    monkeypatch.setattr(chat, "_answer", ans)


async def test_seams_delegate_to_llm_client(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_embed(texts, **kw):
        seen["embed"] = texts
        return [[0.1]]

    async def fake_chat(system, user, **kw):
        seen["chat"] = (system, user)
        return {"answer": "x"}, 3

    monkeypatch.setattr(llm_client, "embed", fake_embed)
    monkeypatch.setattr(llm_client, "chat_json", fake_chat)
    assert await chat._embed_query("why?") == [[0.1]]
    assert await chat._answer("S", "U") == ({"answer": "x"}, 3)
    assert seen == {"embed": ["why?"], "chat": ("S", "U")}


async def test_ask_rejects_disabled_and_blank(db_session, monkeypatch) -> None:
    async with db_session() as s:
        monkeypatch.setattr(settings, "chat_enabled", False)
        assert not chat.is_enabled()
        with pytest.raises(chat.ChatError, match="disabled"):
            await chat.ask(s, "hi", user_id=1, username="u")
        monkeypatch.setattr(settings, "chat_enabled", True)
        with pytest.raises(chat.ChatError, match="Empty question"):
            await chat.ask(s, "   ", user_id=1, username="u")


async def test_retrieve_scopes_to_user_feeds_and_skips_dangling(db_session, store) -> None:
    ids = await _world(db_session, store)
    await store.upsert_story_centroid(9999, [1.0, 0.0])  # centroid of a purged story
    async with db_session() as s:
        stories, _, sims = await chat._retrieve(s, [1.0, 0.0], user_id=ids["u1"])
        assert [st.id for st in stories] == [ids["a"]]  # B belongs to u2, 9999 not subscribed
        assert sims[ids["a"]] == pytest.approx(1.0)

        # unscoped: dangling centroid dropped because the story row is gone
        stories, _, sims = await chat._retrieve(s, [1.0, 0.0])
        assert [st.id for st in stories] == [ids["a"], ids["b"]]
        assert sims[ids["a"]] > sims[ids["b"]]


async def test_retrieve_respects_top_k_and_missing_centroid(db_session, store, monkeypatch) -> None:
    ids = await _world(db_session, store)
    monkeypatch.setattr(settings, "chat_top_k", 1)

    class NoCentroid(InMemoryVectorStore):
        async def get_story_centroid(self, story_id: int):
            return None if story_id == ids["a"] else await super().get_story_centroid(story_id)

    ns = NoCentroid()
    ns.centroids = dict(store.centroids)
    monkeypatch.setattr(chat, "get_vector_store", lambda session=None: ns)
    async with db_session() as s:
        stories, _, _ = await chat._retrieve(s, [1.0, 0.0])
    assert [st.id for st in stories] == [ids["b"]]


async def test_source_hosts_dedupes_strips_www_and_caps(db_session) -> None:
    async with db_session() as s:
        feed = Feed(url="https://f.test/rss")
        story = Story(title="s")
        s.add_all([feed, story])
        await s.flush()
        urls = ["https://www.a.test/1", "https://a.test/2", "/relative"] + [
            f"https://h{i}.test/x" for i in range(8)
        ]
        for i, u in enumerate(urls):
            s.add(Article(feed_id=feed.id, guid=str(i), url=u, story_id=story.id))
        s.add(Article(feed_id=feed.id, guid="orphan", url="https://orphan.test/", story_id=None))
        await s.commit()
        hosts = await chat._source_hosts(s, [story.id])
        assert await chat._source_hosts(s, []) == {}
    assert hosts[story.id][0] == "a.test"
    assert len(hosts[story.id]) == 5 and "orphan.test" not in hosts[story.id]
    assert hosts[story.id].count("a.test") == 1


async def test_ask_happy_path_orders_cited_first_and_persists(
    db_session, store, monkeypatch
) -> None:
    ids = await _world(db_session, store)
    _patch_llm(
        monkeypatch,
        answer={"answer": "  Because B.  ", "story_ids": [ids["b"], 424242, "x", ids["b"]]},
    )
    async with db_session() as s:
        out = await chat.ask(s, " what? ", user_id=ids["u2"], username="u2")
    assert out["answer"] == "Because B."
    assert [(x["id"], x["cited"]) for x in out["stories"]] == [(ids["b"], True)]
    assert out["latency_ms"] == 7

    _patch_llm(monkeypatch, answer={"answer": "mix", "story_ids": [ids["b"]]})
    async with db_session() as s:
        # u1 has only A; give u1 access to B too so both retrieved
        feed = await s.scalar(select(Feed).where(Feed.url == "https://f2.test/rss"))
        s.add(UserFeed(user_id=ids["u1"], feed_id=feed.id))  # type: ignore[union-attr]
        await s.commit()
        out = await chat.ask(s, "what?", user_id=ids["u1"], username="u1")
        assert [(x["id"], x["cited"]) for x in out["stories"]] == [
            (ids["b"], True),
            (ids["a"], False),
        ]
        a_entry = out["stories"][1]
        assert sorted(a_entry["source_hosts"]) == ["one.test", "two.test"]
        hist = await chat.get_history(s, ids["u1"])
        assert [m.role for m in hist] == ["user", "assistant"]
        assert hist[0].content == "what?" and hist[1].content == "mix"
        assert hist[1].latency_ms == 7 and f'"id": {ids["b"]}' in (hist[1].stories_json or "")
        events = (
            await s.scalars(select(ActivityEvent).where(ActivityEvent.component == "chat"))
        ).all()
        assert any('"phase": "done"' in e.detail for e in events)

        await chat.clear_history(s, ids["u1"])
        assert await chat.get_history(s, ids["u1"]) == []
        # other user's history untouched
        assert len(await chat.get_history(s, ids["u2"])) == 2


async def test_ask_no_stories_gives_canned_answer_without_llm(
    db_session, store, monkeypatch
) -> None:
    ids = await _world(db_session, store)

    async def never(system: str, user: str):
        raise AssertionError("LLM must not be called without grounding")

    _patch_llm(monkeypatch)
    monkeypatch.setattr(chat, "_answer", never)
    async with db_session() as s:
        out = await chat.ask(s, "q", user_id=ids["u1"] + 100, username="nobody")
    assert out["stories"] == [] and "couldn't find" in out["answer"]


@pytest.mark.parametrize("vectors", [[], [[]]])
async def test_ask_empty_embedding_fails(db_session, store, monkeypatch, vectors) -> None:
    ids = await _world(db_session, store)
    _patch_llm(monkeypatch, vectors=vectors)
    async with db_session() as s:
        with pytest.raises(chat.ChatError, match="Embedding failed"):
            await chat.ask(s, "q", user_id=ids["u1"], username="u")
        ev = (await s.scalars(select(ActivityEvent).where(ActivityEvent.level == "error"))).all()
        assert len(ev) == 1 and "embed" in ev[0].detail


@pytest.mark.parametrize("answer", [{"answer": "   "}, {"answer": 5}, {"story_ids": []}])
async def test_ask_empty_llm_answer_fails(db_session, store, monkeypatch, answer) -> None:
    ids = await _world(db_session, store)
    _patch_llm(monkeypatch, answer=answer)
    async with db_session() as s:
        with pytest.raises(chat.ChatError, match="empty answer"):
            await chat.ask(s, "q", user_id=ids["u1"], username="u")
        assert await chat.get_history(s, ids["u1"]) == []  # failed turns not persisted


async def test_ask_llm_exception_wrapped(db_session, store, monkeypatch) -> None:
    ids = await _world(db_session, store)
    _patch_llm(monkeypatch, exc=llm_client.LLMError("timeout"))
    async with db_session() as s:
        with pytest.raises(chat.ChatError, match="LLM answer failed: timeout"):
            await chat.ask(s, "q", user_id=ids["u1"], username="u")


async def test_ask_trims_context_to_input_budget(db_session, store, monkeypatch) -> None:
    ids = await _world(db_session, store)
    async with db_session() as s:
        feed = await s.scalar(select(Feed).where(Feed.url == "https://f2.test/rss"))
        s.add(UserFeed(user_id=ids["u1"], feed_id=feed.id))  # type: ignore[union-attr]
        await s.commit()
    prompts_seen: list[str] = []

    async def ans(system: str, user: str):
        prompts_seen.append(user)
        return {"answer": "ok"}, 1

    _patch_llm(monkeypatch)
    monkeypatch.setattr(chat, "_answer", ans)
    monkeypatch.setattr(chat.llm_budget, "input_budget_chars", lambda s, u: 10)
    async with db_session() as s:
        out = await chat.ask(s, "q", user_id=ids["u1"], username="u")
    # best-ranked story always kept, the rest dropped from the prompt (but still listed)
    assert "Story A" in prompts_seen[0] and "Story B" not in prompts_seen[0]
    assert len(out["stories"]) == 2 and not any(x["cited"] for x in out["stories"])


async def test_persist_turn_never_raises() -> None:
    class Broken:
        def add(self, obj: object) -> None:
            raise RuntimeError("db gone")

    chat._persist_turn(Broken(), user_id=1, question="q", answer="a", stories=[], latency_ms=0)  # type: ignore[arg-type]
