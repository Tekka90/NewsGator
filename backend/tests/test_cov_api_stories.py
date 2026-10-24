"""Stories API: visibility rules, list validation/cursors, counts/facets/widget edge cases,
read/unread, merge/move/diff errors, similar-story fallbacks, listen-text and share failures."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.models import (
    ActivityEvent,
    Article,
    Feed,
    Story,
    StoryRevision,
    StoryState,
    User,
    UserFeed,
)
from app.services import listen_text, llm_client, share
from app.services import story_presets as sp
from app.services.vectorstore import InMemoryVectorStore

NOW = datetime.now(UTC)


async def _seed(db_session) -> dict[str, int]:
    """Admin (user 1) follows `mine`; `foreign` belongs to nobody. One story per feed."""
    async with db_session() as s:
        mine = Feed(url="https://mine.example.com/rss", title="Mine")
        foreign = Feed(url="https://foreign.example.com/rss", title="Foreign")
        s.add_all([mine, foreign])
        await s.flush()
        s.add(UserFeed(user_id=1, feed_id=mine.id))
        visible = Story(title="Visible", summary="v", category="tech", version=2)
        hidden = Story(title="Hidden", summary="h", category="sport", version=1)
        extra = Story(title="Extra", summary="e", category="tech", version=1)
        s.add_all([visible, hidden, extra])
        await s.flush()
        arts = []
        for i, (story, feed) in enumerate([(visible, mine), (hidden, foreign), (extra, mine)]):
            a = Article(
                feed_id=feed.id, guid=f"g{i}", url=f"https://www.site{i}.example.com/{i}",
                title=f"A{i}", story_id=story.id, processing_state="clustered",
                published_at=NOW - timedelta(days=i), full_text="x" * 400,
            )
            arts.append(a)
        s.add_all(arts)
        await s.commit()
        return {
            "mine": mine.id, "foreign": foreign.id, "visible": visible.id, "hidden": hidden.id,
            "extra": extra.id, "a_visible": arts[0].id, "a_hidden": arts[1].id,
        }


async def test_hidden_story_is_404_everywhere_for_non_subscriber(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    h, a = ids["hidden"], ids["a_hidden"]

    assert (await client.get(f"/api/stories/{h}")).status_code == 404
    assert (await client.put(f"/api/stories/{h}/saved")).status_code == 404
    assert (await client.delete(f"/api/stories/{h}/saved")).status_code == 404
    assert (await client.get(f"/api/stories/{h}/listen-position")).status_code == 404
    assert (await client.delete(f"/api/stories/{h}/listen-position")).status_code == 404
    assert (await client.get(f"/api/stories/articles/{a}/listen-text")).status_code == 404
    assert (await client.get("/api/stories/999999")).status_code == 404

    listing = (await client.get("/api/stories")).json()
    assert sorted(s["title"] for s in listing) == ["Extra", "Visible"]


async def test_list_validation_and_empty_scopes(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)

    r = await client.get("/api/stories?limit=2&before=1&after=2")
    assert r.status_code == 422
    r = await client.get("/api/stories?before=1")
    assert r.status_code == 422 and "limit" in r.json()["detail"]
    assert (await client.get("/api/stories?preset=nope")).status_code == 404
    assert (await client.get("/api/stories?filter=bogus")).status_code == 422

    # Feeds the user does not follow never leak stories
    assert (await client.get(f"/api/stories?feed={ids['foreign']}")).json() == []
    assert (await client.get(f"/api/stories?feed_ids={ids['foreign']}")).json() == []
    only_mine = await client.get(f"/api/stories?feed_ids={ids['mine']}&feed_ids={ids['foreign']}")
    assert len(only_mine.json()) == 2

    assert (await client.get("/api/stories?category=sport")).json() == []
    assert len((await client.get("/api/stories?category=tech")).json()) == 2


async def test_list_empty_for_user_without_subscriptions(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    await _seed(db_session)
    await client.post("/api/users", json={"username": "lonely", "password": "lonelypass1"})
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "lonely", "password": "lonelypass1"})

    assert (await client.get("/api/stories")).json() == []
    assert (await client.get("/api/stories/counts")).json() == {
        "all": 0, "unread": 0, "updated": 0, "saved": 0,
    }
    assert (await client.get("/api/stories/facets")).json() == {"categories": [], "feeds": []}
    snap = (
        await client.get("/api/stories/widget-snapshot", params={"since": NOW.isoformat()})
    ).json()
    assert snap["unread_count"] == 0 and all(v == [] for v in snap["lists"].values())
    assert (await client.get("/api/stories/feed-options")).json() == []
    # Default (enabled) presets still report a zero count each
    counts = (await client.get("/api/stories/preset-counts")).json()["counts"]
    assert counts and set(counts.values()) == {0}


async def test_counts_scope_by_feed_and_category(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    zero = {"all": 0, "unread": 0, "updated": 0, "saved": 0}
    assert (await client.get(f"/api/stories/counts?feed={ids['foreign']}")).json() == zero
    assert (await client.get("/api/stories/counts?category=sport")).json() == zero
    assert (await client.get("/api/stories/counts?category=tech")).json()["all"] == 2


async def test_include_read_keeps_read_stories_reachable(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    await client.post(f"/api/stories/{ids['visible']}/read")

    unread = {s["id"] for s in (await client.get("/api/stories?filter=unread")).json()}
    assert unread == {ids["extra"]}
    with_read = {
        s["id"] for s in (await client.get("/api/stories?filter=unread&include_read=true")).json()
    }
    assert with_read == {ids["extra"], ids["visible"]}


async def test_cursor_paging_before_after_and_unknown_cursor(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    order = [s["id"] for s in (await client.get("/api/stories?sort=published&order=asc")).json()]
    assert order == [ids["extra"], ids["visible"]]  # extra is older

    after = await client.get(f"/api/stories?sort=published&order=asc&limit=5&after={order[0]}")
    assert [s["id"] for s in after.json()] == [order[1]]
    before = await client.get(f"/api/stories?sort=published&order=asc&limit=5&before={order[1]}")
    assert [s["id"] for s in before.json()] == [order[0]]
    assert (await client.get(f"/api/stories?limit=5&after={ids['hidden']}")).json() == []


async def test_preset_with_unmatched_source_kind_is_empty(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    await _seed(db_session)  # no newsletter feed anywhere
    preset = {
        "id": "mail", "kind": "custom", "label": "Mail", "icon": "star",
        "display": "icon+label", "enabled": True, "position": 4, "sort": "published",
        "order": "desc", "show_count": True,
        "criteria": {**sp.empty_criteria(), "source_kinds": ["mail"]},
    }
    r = await client.put("/api/me/presets", json={"presets": [*sp.default_presets(), preset]})
    assert r.status_code == 200, r.text
    assert (await client.get("/api/stories?preset=mail")).json() == []
    assert (await client.get("/api/stories/preset-counts")).json()["counts"]["mail"] == 0


async def test_widget_snapshot_ranks_by_recent_sources(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    async with db_session() as s:
        # visible story gets 2 extra fresh sources; extra story's only source is old
        for i in range(2):
            s.add(
                Article(
                    feed_id=ids["mine"], guid=f"w{i}", url=f"https://w{i}.example.com/",
                    title="w", story_id=ids["visible"], published_at=NOW,
                    processing_state="clustered",
                )
            )
        await s.commit()
    since = (NOW - timedelta(hours=12)).isoformat()
    snap = (await client.get("/api/stories/widget-snapshot", params={"since": since})).json()
    assert snap["unread_count"] == 2
    # Only stories with a source inside the window rank under most_sources
    assert [s["id"] for s in snap["lists"]["most_sources"]] == [ids["visible"]]
    assert {s["id"] for s in snap["lists"]["latest"]} == {ids["visible"], ids["extra"]}
    assert snap["lists"]["oldest"][0]["id"] == ids["extra"]

    await client.post(f"/api/stories/{ids['visible']}/read")
    snap = (await client.get("/api/stories/widget-snapshot", params={"since": since})).json()
    assert snap["unread_count"] == 1
    assert snap["lists"]["most_sources_unread"] == []
    assert [s["id"] for s in snap["lists"]["latest_unread"]] == [ids["extra"]]


async def test_mark_read_unread_cycle_and_missing(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    sid = ids["visible"]

    assert (await client.post("/api/stories/999999/read")).status_code == 404
    # Unread on a never-read story is a harmless no-op
    assert (await client.post(f"/api/stories/{sid}/unread")).status_code == 204
    async with db_session() as s:
        assert await s.get(StoryState, (1, sid)) is None

    assert (await client.post(f"/api/stories/{sid}/read")).status_code == 204
    detail = (await client.get(f"/api/stories/{sid}")).json()
    assert detail["is_read"] is True and detail["updated_since_read"] is False
    async with db_session() as s:
        assert (await s.get(StoryState, (1, sid))).read_at_version == 2  # type: ignore[union-attr]

    assert (await client.post(f"/api/stories/{sid}/unread")).status_code == 204
    assert (await client.get(f"/api/stories/{sid}")).json()["is_read"] is False


async def test_saved_survives_unread_and_is_idempotent(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    sid = ids["visible"]
    first = (await client.put(f"/api/stories/{sid}/saved")).json()
    second = (await client.put(f"/api/stories/{sid}/saved")).json()
    assert first["saved"] is True and first["saved_at"] == second["saved_at"]
    detail = (await client.get(f"/api/stories/{sid}")).json()
    assert detail["saved"] is True and detail["is_read"] is False

    assert (await client.delete(f"/api/stories/{sid}/saved")).json() == {
        "saved": False, "saved_at": None,
    }
    # Clearing a story that was never saved is fine too
    assert (await client.delete(f"/api/stories/{ids['extra']}/saved")).json()["saved"] is False


async def test_diff_lists_newer_revisions_and_404s(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    async with db_session() as s:
        for v in (1, 2, 3):
            s.add(StoryRevision(story_id=ids["visible"], version=v, summary=f"rev{v}"))
        await s.commit()
    body = (await client.get(f"/api/stories/{ids['visible']}/diff?from=1")).json()
    assert body["from_version"] == 1
    assert [c["summary"] for c in body["changes"]] == ["rev2", "rev3"]
    # Up-to-date story: empty change list, not an error
    body = (await client.get(f"/api/stories/{ids['visible']}/diff?from=3")).json()
    assert body["changes"] == []
    assert (await client.get("/api/stories/999999/diff?from=1")).status_code == 404
    assert (await client.get(f"/api/stories/{ids['visible']}/diff?from=0")).status_code == 422


async def test_merge_errors_and_effects(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import stories as stories_api

    store = InMemoryVectorStore()
    monkeypatch.setattr(stories_api, "get_vector_store", lambda session=None: store)
    await setup_admin(client)
    ids = await _seed(db_session)
    t, src = ids["visible"], ids["extra"]

    r = await client.post(f"/api/stories/{t}/merge", json={"source_story_id": t})
    assert r.status_code == 400
    r = await client.post(f"/api/stories/{t}/merge", json={"source_story_id": 999999})
    assert r.status_code == 404
    r = await client.post("/api/stories/999999/merge", json={"source_story_id": src})
    assert r.status_code == 404

    r = await client.post(f"/api/stories/{t}/merge", json={"source_story_id": src})
    assert r.status_code == 204
    async with db_session() as s:
        assert await s.get(Story, src) is None
        moved = (await s.scalars(select(Article).where(Article.story_id == t))).all()
        assert len(moved) == 2
    assert len((await client.get(f"/api/stories/{t}")).json()["articles"]) == 2


async def test_move_article_errors_and_effects(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    a = ids["a_visible"]
    r = await client.post("/api/stories/articles/999999/move", json={"story_id": 1})
    assert r.status_code == 404
    r = await client.post(f"/api/stories/articles/{a}/move", json={"story_id": 999999})
    assert r.status_code == 404

    r = await client.post(f"/api/stories/articles/{a}/move", json={"story_id": ids["extra"]})
    assert r.status_code == 204
    async with db_session() as s:
        assert (await s.get(Article, a)).story_id == ids["extra"]  # type: ignore[union-attr]
        from app.models import OverridePair

        labels = {
            (p.story_id, p.label) for p in (await s.scalars(select(OverridePair))).all()
        }
    assert labels == {(ids["visible"], "different"), (ids["extra"], "same")}


async def test_similar_candidates_fall_back_to_recent_without_vectors(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import stories as stories_api

    store = InMemoryVectorStore()
    monkeypatch.setattr(stories_api, "get_vector_store", lambda session=None: store)
    await setup_admin(client)
    ids = await _seed(db_session)

    r = await client.get(f"/api/stories/{ids['visible']}/similar")
    got = r.json()
    assert {c["id"] for c in got} == {ids["hidden"], ids["extra"]}  # never the story itself
    assert all(c["similarity"] is None for c in got)
    r = await client.get(f"/api/stories/articles/{ids['a_visible']}/similar-stories")
    assert ids["visible"] not in {c["id"] for c in r.json()}

    assert (await client.get("/api/stories/999999/similar")).status_code == 404
    assert (await client.get("/api/stories/articles/999999/similar-stories")).status_code == 404


async def test_similar_ranks_by_cosine_and_skips_stories_without_centroid(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import stories as stories_api

    store = InMemoryVectorStore()
    monkeypatch.setattr(stories_api, "get_vector_store", lambda session=None: store)
    await setup_admin(client)
    ids = await _seed(db_session)
    await store.upsert_story_centroid(ids["visible"], [1.0, 0.0])
    await store.upsert_story_centroid(ids["extra"], [0.9, 0.1])
    await store.upsert_story_centroid(ids["hidden"], [0.0, 1.0])
    # A centroid whose story row no longer exists is skipped, not an error
    await store.upsert_story_centroid(424242, [1.0, 0.0])

    got = (await client.get(f"/api/stories/{ids['visible']}/similar")).json()
    assert [c["id"] for c in got] == [ids["extra"], ids["hidden"]]
    assert got[0]["similarity"] > 0.99 and abs(got[1]["similarity"]) < 1e-6


async def test_listen_text_translation_failures_map_to_http_errors(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)
    async with db_session() as s:
        a = await s.get(Article, ids["a_visible"])
        a.language = "fr"  # type: ignore[union-attr]
        a.content_status = "full"  # type: ignore[union-attr]
        a.full_text = "Un texte suffisamment long. " * 40  # type: ignore[union-attr]
        user = await s.get(User, 1)
        user.summary_language = "en"  # type: ignore[union-attr]
        await s.commit()
    url = f"/api/stories/articles/{ids['a_visible']}/listen-text?lang=summary"

    def raiser(exc: Exception) -> Any:
        async def _t(*a: Any, **k: Any) -> str:
            raise exc

        return _t

    monkeypatch.setattr(
        listen_text, "translate_article_text", raiser(listen_text.TranslationTooLongError("big"))
    )
    r = await client.get(url)
    assert r.status_code == 422
    monkeypatch.setattr(listen_text, "translate_article_text", raiser(llm_client.LLMError("x")))
    r = await client.get(url)
    assert r.status_code == 502


async def test_share_provider_failure_is_502_and_logged(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session)

    async def failing(story: Any, articles: Any, language: Any) -> Any:
        raise llm_client.LLMError("upstream down")

    monkeypatch.setattr(share, "prepare_share", failing)
    r = await client.post(f"/api/stories/{ids['visible']}/share", json={"language": "fr"})
    assert r.status_code == 502
    assert "upstream down" in r.json()["detail"]
    async with db_session() as s:
        events = (
            await s.scalars(select(ActivityEvent).where(ActivityEvent.component == "share"))
        ).all()
    assert [e.action for e in events] == ["prepare_start", "prepare_failed"]
    assert (await client.post("/api/stories/999999/share", json={})).status_code == 404


async def test_reprocess_missing_article_is_404(client: AsyncClient) -> None:
    await setup_admin(client)
    assert (await client.post("/api/stories/articles/999999/reprocess")).status_code == 404
