"""Topic Radar: pure matching contract, discovery sweep, rules, story-list filtering."""

import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.api import stories as stories_api
from app.api import topics as topics_api
from app.core.config import settings
from app.models import Article, Feed, Story, StoryTopic, Topic, UserFeed, utcnow
from app.services import topics as topics_svc
from app.services.vectorstore import InMemoryVectorStore

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "topic_vectors.json").read_text(encoding="utf-8")
)


def test_rule_matches_vectors() -> None:
    for case in VECTORS["rule_matches"]:
        got = topics_svc.rule_matches(
            np.array(case["story"], float), np.array(case["rule"], float), case["threshold"]
        )
        assert got is case["expected"], case


def test_visibility_vectors() -> None:
    for case in VECTORS["visibility"]:
        assert topics_svc.visibility(case["modes"], saved=case["saved"]) == case["expected"], case


def test_cluster_vectors_contract() -> None:
    spec = VECTORS["clusters"]
    vectors = {int(k): np.array(v, float) for k, v in spec["vectors"].items()}
    got = [members for _, members in topics_svc.cluster_vectors(vectors, spec["threshold"])]
    assert got == spec["expected_members"]


def test_encode_roundtrip() -> None:
    assert topics_svc.decode(topics_svc.encode([0.5, -1.0, 2.0])).tolist() == [0.5, -1.0, 2.0]


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVectorStore:
    s = InMemoryVectorStore()
    for module in (topics_svc, topics_api, stories_api):
        monkeypatch.setattr(module, "get_vector_store", lambda session=None: s)
    monkeypatch.setattr(settings, "topic_min_stories", 3)
    monkeypatch.setattr(settings, "topic_min_sources", 2)
    monkeypatch.setattr(settings, "topic_min_user_share", 0.0)
    monkeypatch.setattr(topics_svc.llm_client, "is_configured", lambda: False)
    return s


def _vec(on: int, dim: int = 8, jitter: float = 0.0) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[on % dim] = 1.0
    v[(on + 1) % dim] = jitter
    return v.tolist()


async def _seed(db_session, store: InMemoryVectorStore) -> list[int]:
    """Five stories on axis 0 (the topic) and two unrelated ones, from distinct hosts."""
    async with db_session() as s:
        feed = Feed(url="https://t.example.com/rss", title="T")
        s.add(feed)
        await s.flush()
        s.add(UserFeed(user_id=1, feed_id=feed.id))
        ids: list[int] = []
        for i in range(7):
            story = Story(title=f"Story {i}", summary="x", version=1, category="Tech")
            s.add(story)
            await s.flush()
            ids.append(story.id)
            s.add(
                Article(
                    feed_id=feed.id,
                    guid=f"t-{i}",
                    url=f"https://host{i}.example.com/{i}",
                    title=f"A{i}",
                    story_id=story.id,
                    processing_state="clustered",
                )
            )
            await store.upsert_story_centroid(
                story.id, _vec(0, jitter=0.05 * i) if i < 5 else _vec(i)
            )
        await s.commit()
    return ids


async def _discover(db_session) -> dict[str, int]:
    async with db_session() as s:
        return await topics_svc.refresh_topics(s)


async def test_refresh_discovers_topic(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, store)
    report = await _discover(db_session)
    assert report["created"] == 1
    async with db_session() as s:
        topic = (await s.scalars(select(Topic))).one()
        members = set((await s.scalars(select(StoryTopic.story_id))).all())
    assert topic.label.startswith("Story") and topic.story_count == 5
    assert members == set(ids[:5])
    # a second sweep reuses the topic instead of creating another
    assert (await _discover(db_session))["created"] == 0
    listed = (await client.get("/api/topics")).json()
    assert len(listed) == 1 and listed[0]["unread_count"] == 5 and listed[0]["rule"] is None


def test_layout_positions_are_bounded_and_keep_neighbours_close() -> None:
    a1, a2, b = np.array([1.0, 0, 0, 0]), np.array([0.95, 0.1, 0, 0]), np.array([0, 0, 1.0, 0])
    pos = topics_svc.layout_positions({1: a1, 2: a2, 3: b})
    assert set(pos) == {1, 2, 3}
    assert all(abs(c) <= 1.0 + 1e-9 for p in pos.values() for c in p)
    near = float(np.linalg.norm(np.subtract(pos[1], pos[2])))
    far = float(np.linalg.norm(np.subtract(pos[1], pos[3])))
    assert near < far
    assert topics_svc.layout_positions({}) == {}
    assert topics_svc.layout_positions({7: a1}) == {7: (0.0, 0.0, 0.0)}


async def test_topics_all_returns_positions(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    await _seed(db_session, store)
    await _discover(db_session)
    listed = (await client.get("/api/topics", params={"all": "true"})).json()
    assert len(listed) == 1 and len(listed[0]["position"]) == 3
    assert (await client.get("/api/topics")).json()[0]["position"] is None


async def test_mute_hides_and_unmute_restores(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]

    preview = (await client.get(f"/api/topics/{topic_id}/preview")).json()
    assert preview["would_hide_unread"] == 5 and len(preview["samples"]) == 5

    await client.put(f"/api/stories/{ids[0]}/saved")  # saved stories are never ignored
    r = await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["matched_stories"] == 5 and rule["until_fade"] is True

    visible = {s["id"] for s in (await client.get("/api/stories")).json()}
    assert visible == {ids[0], ids[5], ids[6]}
    counts = (await client.get("/api/stories/counts")).json()
    assert counts["all"] == 3 and counts["ignored"] == 4 and counts["saved"] == 1
    only = (await client.get("/api/stories?ignored=only")).json()
    assert {s["id"] for s in only} == set(ids[1:5]) and all(s["ignored"] for s in only)
    everything = (await client.get("/api/stories?ignored=include")).json()
    assert len(everything) == 7

    assert (await client.delete(f"/api/topic-rules/{rule['id']}")).status_code == 204
    assert len((await client.get("/api/stories")).json()) == 7
    assert (await client.get("/api/stories/counts")).json()["ignored"] == 0


async def test_follow_beats_mute_and_rule_expiry(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "follow", "duration": "30d"})
    listed = (await client.get("/api/stories")).json()
    assert len(listed) == 7 and sum(s["followed"] for s in listed) == 5
    assert [r["mode"] for r in (await client.get("/api/topic-rules")).json()] == ["follow"]
    # replacing the rule keeps one rule per topic
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute", "duration": "7d"})
    assert [r["mode"] for r in (await client.get("/api/topic-rules")).json()] == ["mute"]
    assert len((await client.get("/api/stories")).json()) == 2
    assert ids


async def test_rule_ends_when_topic_fades(
    client: AsyncClient, db_session, store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})
    assert len((await client.get("/api/stories")).json()) == 2
    # nothing recent enough to keep the topic alive: it fades and its "until fade" rule ends
    monkeypatch.setattr(settings, "topic_window_days", 0)
    monkeypatch.setattr(settings, "topic_fade_days", 0)
    async with db_session() as s:
        topic = await s.get(Topic, topic_id)
        assert topic is not None
        topic.last_story_at = utcnow() - timedelta(days=1)
        await s.commit()
    report = await _discover(db_session)
    assert report["faded"] == 1 and report["rules_expired"] == 1
    assert len((await client.get("/api/stories")).json()) == 7


async def test_feedback_tightens_rule(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    rule = (await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})).json()
    out = (
        await client.post(f"/api/topic-rules/{rule['id']}/feedback", json={"story_id": ids[4]})
    ).json()
    assert out["matched_stories"] < 5
    assert ids[4] in {s["id"] for s in (await client.get("/api/stories")).json()}


async def test_rules_are_private(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    rule = (await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})).json()
    await client.post("/api/users", json={"username": "other", "password": "otherpass12"})
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "other", "password": "otherpass12"})
    assert (await client.delete(f"/api/topic-rules/{rule['id']}")).status_code == 404
    assert (await client.get("/api/topic-rules")).json() == []


async def test_new_story_is_hidden_by_existing_rule(client: AsyncClient, db_session, store) -> None:
    """Fast path after clustering: a story arriving later joins the topic and the mute rule."""
    await setup_admin(client)
    await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})
    async with db_session() as s:
        feed = (await s.scalars(select(Feed))).one()
        late = Story(title="Late", summary="x", version=1, category="Tech")
        s.add(late)
        await s.flush()
        s.add(
            Article(
                feed_id=feed.id,
                guid="late",
                url="https://late.example.com/x",
                title="L",
                story_id=late.id,
                processing_state="clustered",
            )
        )
        await store.upsert_story_centroid(late.id, _vec(0, jitter=0.02))
        await topics_svc.evaluate_story(s, late.id)
        await s.commit()
        late_id = late.id
        assigned = set((await s.scalars(select(StoryTopic.story_id))).all())
    assert late_id in assigned
    assert late_id not in {st["id"] for st in (await client.get("/api/stories")).json()}


async def test_manual_topic_follow_filter_and_mode_switch(
    client: AsyncClient, db_session, store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, store)

    async def fake_embed(texts: list[str], *, model: str | None = None) -> list[list[float]]:
        return [_vec(0)]

    monkeypatch.setattr(topics_svc.llm_client, "embed", fake_embed)
    r = await client.post("/api/topic-rules/custom", json={"query": "  My   subject "})
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["mode"] == "follow" and rule["label"] == "My subject"
    assert rule["topic_id"] is None and rule["matched_stories"] == 5

    star = (await client.get("/api/topics", params={"all": "true"})).json()[0]
    assert star["manual"] is True and star["id"] == -rule["id"] and len(star["position"]) == 3
    assert star["story_count"] == 5 and star["unread_count"] == 5

    followed = (await client.get("/api/stories", params={"followed": "true"})).json()
    assert {s["id"] for s in followed} == set(ids[:5]) and all(s["followed"] for s in followed)
    assert (await client.get("/api/stories/counts")).json()["followed"] == 5

    r = await client.patch(f"/api/topic-rules/{rule['id']}", json={"mode": "mute"})
    assert r.status_code == 200 and r.json()["mode"] == "mute"
    assert len((await client.get("/api/stories")).json()) == 2
    assert (await client.get("/api/stories/counts")).json()["followed"] == 0

    # the same phrase again replaces the rule instead of duplicating it
    await client.post("/api/topic-rules/custom", json={"query": "my subject", "mode": "follow"})
    assert len((await client.get("/api/topic-rules")).json()) == 1
    assert (await client.post("/api/topic-rules/custom", json={"query": "x"})).status_code == 422
    new_id = (await client.get("/api/topic-rules")).json()[0]["id"]
    assert (await client.delete(f"/api/topic-rules/{new_id}")).status_code == 204
    assert (await client.get("/api/topics", params={"all": "true"})).json() == []
