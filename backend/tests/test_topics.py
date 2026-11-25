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
from app.models import (
    Article,
    Feed,
    Story,
    StorySetAside,
    StoryTopic,
    Topic,
    UserFeed,
    utcnow,
)
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
    preset_counts = (await client.get("/api/stories/preset-counts")).json()["counts"]
    # Hidden is disabled by default, so it has no badge count until the user enables it.
    assert "hidden" not in preset_counts
    hidden_view = await client.get("/api/stories?preset=hidden")
    assert hidden_view.status_code == 404
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
    async with db_session() as s:
        # Semantically close, but assigned elsewhere. It is covered by the rule,
        # not part of the topic's primary membership.
        await store.upsert_story_centroid(ids[5], _vec(0, jitter=0.02))
        await s.commit()
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "follow", "duration": "30d"})
    listed = (await client.get("/api/stories")).json()
    assert len(listed) == 7 and sum(s["followed"] for s in listed) == 6
    assert ids[5] in {s["id"] for s in listed if s["followed"]}
    assert [r["mode"] for r in (await client.get("/api/topic-rules")).json()] == ["follow"]
    topic = (await client.get("/api/topics")).json()[0]
    assert topic["story_count"] == 5 and topic["unread_count"] == 5
    assert topic["covered_story_count"] == 6 and topic["covered_unread_count"] == 6
    groups = (await client.get("/api/topic-groups")).json()
    if groups:
        assert sum(g["followed_topic_count"] for g in groups) == 1
    # replacing the rule keeps one rule per topic
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute", "duration": "7d"})
    assert [r["mode"] for r in (await client.get("/api/topic-rules")).json()] == ["mute"]
    assert len((await client.get("/api/stories")).json()) == 1
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
    # the muted stories are set aside, not released
    assert len((await client.get("/api/stories")).json()) == 2


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
    assert star["covered_story_count"] == 5 and star["covered_unread_count"] == 5

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


# --- topic groups (galaxies) ---


def _unit(*pairs: tuple[int, float], dim: int = 8) -> np.ndarray:
    v = np.zeros(dim)
    for axis, value in pairs:
        v[axis] = value
    return v


def test_group_topics_separates_caps_and_is_deterministic(monkeypatch) -> None:
    near = {
        1: _unit((0, 1)),
        2: _unit((0, 1), (1, 0.3)),
        3: _unit((4, 1)),
        4: _unit((4, 1), (5, 0.2)),
    }
    assert topics_svc.group_topics(near) == [[1, 2], [3, 4]]
    assert topics_svc.group_topics({k: near[k] for k in (4, 3, 2, 1)}) == [[1, 2], [3, 4]]
    assert topics_svc.group_topics({}) == []
    assert topics_svc.group_topics({9: near[1]}) == [[9]]
    monkeypatch.setattr(settings, "topic_group_max", 3)
    assert topics_svc.group_topics(near) == [[1, 2], [3, 4]]
    spread = {i: _unit((i, 1)) for i in range(6)}  # orthogonal: nothing merges by threshold
    monkeypatch.setattr(settings, "topic_group_max", 12)
    assert len(topics_svc.group_topics(spread)) == 6
    monkeypatch.setattr(settings, "topic_group_max", 2)
    capped = topics_svc.group_topics(spread)
    assert len(capped) == 2 and sorted(m for g in capped for m in g) == list(range(6))
    assert capped == topics_svc.group_topics(spread)


async def _seed_topic(db_session, store: InMemoryVectorStore, axis: int, first: int) -> list[int]:
    async with db_session() as s:
        feed = (await s.scalars(select(Feed))).first()
        assert feed is not None
        ids: list[int] = []
        for i in range(first, first + 5):
            story = Story(title=f"Extra {i}", summary="x", version=1, category="Tech")
            s.add(story)
            await s.flush()
            ids.append(story.id)
            s.add(
                Article(
                    feed_id=feed.id,
                    guid=f"x-{i}",
                    url=f"https://extra{i}.example.com/{i}",
                    title=f"E{i}",
                    story_id=story.id,
                    processing_state="clustered",
                )
            )
            await store.upsert_story_centroid(story.id, _vec(axis, jitter=0.02 * i))
        await s.commit()
    return ids


async def test_story_detail_reports_radar_topic_and_galaxy(
    client: AsyncClient, db_session, store
) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, store)
    await _discover(db_session)
    topic = (await client.get("/api/topics", params={"all": "true"})).json()[0]
    group = (await client.get("/api/topic-groups")).json()[0]
    radar = (await client.get(f"/api/stories/{ids[0]}")).json()["radar"]
    assert radar["topic"]["id"] == topic["id"] and radar["topic"]["label"] == topic["label"]
    assert 0.0 < radar["topic"]["score"] <= 1.0
    assert radar["group"]["id"] == group["id"] and 0.0 < radar["group"]["score"] <= 1.0
    # a story outside every topic has no radar block
    assert (await client.get(f"/api/stories/{ids[-1]}")).json()["radar"] is None


async def test_groups_api_scoping_and_identity(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    await _seed(db_session, store)
    await _seed_topic(db_session, store, 3, 10)
    report = await _discover(db_session)
    assert report["created"] == 2 and report["groups_created"] == 2
    groups = (await client.get("/api/topic-groups")).json()
    assert len(groups) == 2
    first = groups[0]
    assert first["topic_count"] == 1 and first["story_count"] == 5 and first["unread_count"] == 5
    assert first["hue"] == (first["id"] * 137) % 360 and len(first["position"]) == 3
    assert first["rule"] is None and first["label"]

    topics = (await client.get("/api/topics", params={"all": "true"})).json()
    assert {t["group_id"] for t in topics} == {g["id"] for g in groups}
    only = (
        await client.get("/api/topics", params={"all": "true", "group": first["id"]})
    ).json()
    assert len(only) == 1 and only[0]["group_id"] == first["id"] and len(only[0]["position"]) == 3

    # ids and labels survive a second sweep
    assert (await _discover(db_session))["groups_created"] == 0
    again = (await client.get("/api/topic-groups")).json()
    assert [(g["id"], g["label"]) for g in again] == [(g["id"], g["label"]) for g in groups]


async def test_group_rule_mutes_live_and_replaces(client: AsyncClient, db_session, store) -> None:
    await setup_admin(client)
    ids = await _seed(db_session, store)
    await _seed_topic(db_session, store, 3, 10)
    await _discover(db_session)
    listed = (await client.get("/api/topics", params={"all": "true"})).json()
    topic = next(t for t in listed if t["label"].startswith("Story"))
    gid = topic["group_id"]
    preview = (await client.get(f"/api/topic-groups/{gid}/preview")).json()
    assert preview["would_hide_unread"] == 5 and len(preview["samples"]) == 5

    r = await client.post(f"/api/topic-groups/{gid}/rules", json={"mode": "mute"})
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["group_id"] == gid and rule["topic_id"] is None and rule["until_fade"] is True
    visible = {s["id"] for s in (await client.get("/api/stories")).json()}
    assert not visible & set(ids[:5]) and len(visible) == 7  # 2 plain + 5 of the other topic
    group = next(g for g in (await client.get("/api/topic-groups")).json() if g["id"] == gid)
    assert group["rule"]["id"] == rule["id"] and group["rule"]["mode"] == "mute"
    assert group["muted_topic_count"] == 0
    assert (await client.get("/api/topic-rules")).json()[0]["group_id"] == gid
    # a typed-topic list is not polluted by the group rule
    manual = await client.get("/api/topics", params={"all": "true", "manual_only": "true"})
    assert manual.json() == []

    # a new story close to the group is hidden at once (live membership, no sweep)
    async with db_session() as s:
        story = Story(title="Fresh", summary="x", version=1, category="Tech")
        s.add(story)
        await s.flush()
        await store.upsert_story_centroid(story.id, _vec(0, jitter=0.1))
        await topics_svc.evaluate_story(s, story.id)
        await s.commit()
        fresh = story.id
    assert fresh not in {s["id"] for s in (await client.get("/api/stories")).json()}

    again = await client.post(f"/api/topic-groups/{gid}/rules", json={"mode": "follow"})
    assert again.json()["mode"] == "follow"
    listed = (await client.get("/api/topic-rules")).json()
    assert len(listed) == 1 and listed[0]["mode"] == "follow"
    missing = await client.post("/api/topic-groups/999/rules", json={"mode": "mute"})
    assert missing.status_code == 404


async def test_group_rule_ends_when_group_disappears(
    client: AsyncClient, db_session, store
) -> None:
    await setup_admin(client)
    await _seed(db_session, store)
    await _discover(db_session)
    gid = (await client.get("/api/topic-groups")).json()[0]["id"]
    await client.post(f"/api/topic-groups/{gid}/rules", json={"mode": "mute"})
    async with db_session() as s:
        for t in (await s.scalars(select(Topic))).all():
            t.status = "faded"
        for story in (await s.scalars(select(Story))).all():
            story.last_updated_at = utcnow() - timedelta(days=90)
        await s.commit()
    report = await _discover(db_session)
    assert report["groups_deleted"] == 1
    assert (await client.get("/api/topic-groups")).json() == []
    assert (await client.get("/api/topic-rules")).json() == []


async def _muted(client: AsyncClient, db_session, store) -> tuple[list[int], int]:
    await setup_admin(client)
    ids = await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    r = await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute", "duration": "7d"})
    return ids, r.json()["id"]


async def _expire_rules(db_session) -> None:
    from app.models import TopicRule

    async with db_session() as s:
        for rule in (await s.scalars(select(TopicRule))).all():
            rule.expires_at = utcnow() - timedelta(hours=1)
        await s.commit()
    assert (await _discover(db_session))["rules_expired"] == 1


async def test_rule_expiry_sets_stories_aside(client: AsyncClient, db_session, store) -> None:
    ids, _ = await _muted(client, db_session, store)
    rules = (await client.get("/api/topic-rules")).json()
    assert rules[0]["hidden_count"] == 5 and rules[0]["hidden_unread"] == 5
    await _expire_rules(db_session)
    assert (await client.get("/api/topic-rules")).json() == []
    assert {s["id"] for s in (await client.get("/api/stories")).json()} == {ids[5], ids[6]}
    counts = (await client.get("/api/stories/counts")).json()
    assert counts["all"] == 2 and counts["unread"] == 2 and counts["ignored"] == 5
    only = (await client.get("/api/stories?ignored=only")).json()
    assert {s["id"] for s in only} == set(ids[:5]) and all(s["ignored"] for s in only)
    assert (await client.get(f"/api/stories/{ids[0]}")).json()["ignored"] is True
    assert (await client.get(f"/api/stories/{ids[5]}")).json()["ignored"] is False
    # a second sweep is idempotent
    await _discover(db_session)
    async with db_session() as s:
        assert len((await s.scalars(select(StorySetAside))).all()) == 5


async def test_expired_follow_rule_sets_nothing_aside(
    client: AsyncClient, db_session, store
) -> None:
    await setup_admin(client)
    await _seed(db_session, store)
    await _discover(db_session)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "follow", "duration": "7d"})
    await _expire_rules(db_session)
    async with db_session() as s:
        assert (await s.scalars(select(StorySetAside))).all() == []
    assert len((await client.get("/api/stories")).json()) == 7


async def test_delete_rule_keep_hidden(client: AsyncClient, db_session, store) -> None:
    ids, rule_id = await _muted(client, db_session, store)
    assert (
        await client.delete(f"/api/topic-rules/{rule_id}", params={"keep_hidden": "true"})
    ).status_code == 204
    assert {s["id"] for s in (await client.get("/api/stories")).json()} == {ids[5], ids[6]}
    assert (await client.get("/api/stories/counts")).json()["ignored"] == 5

    # POST /remove with a JSON body behaves the same
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    await client.post("/api/stories/unhide-all")
    r = await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})
    rid = r.json()["id"]
    gone = await client.post(f"/api/topic-rules/{rid}/remove", json={"keep_hidden": True})
    assert gone.status_code == 204 and len((await client.get("/api/stories")).json()) == 2
    await client.post("/api/stories/unhide-all")
    r = await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})
    rid = r.json()["id"]
    assert (await client.post(f"/api/topic-rules/{rid}/remove", json={})).status_code == 204
    assert len((await client.get("/api/stories")).json()) == 7

    # default: everything comes back (fresh mute, then plain delete)
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    r = await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})
    await client.post("/api/stories/unhide-all")
    r = await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "mute"})
    assert (await client.delete(f"/api/topic-rules/{r.json()['id']}")).status_code == 204
    assert len((await client.get("/api/stories")).json()) == 7


async def test_unhide_one_and_all(client: AsyncClient, db_session, store) -> None:
    ids, rule_id = await _muted(client, db_session, store)
    # still under a live rule: unhide drops the mute match for that story only
    assert (await client.post(f"/api/stories/{ids[0]}/unhide")).status_code == 204
    assert ids[0] in {s["id"] for s in (await client.get("/api/stories")).json()}
    assert len((await client.get("/api/stories?ignored=only")).json()) == 4

    await client.delete(f"/api/topic-rules/{rule_id}", params={"keep_hidden": "true"})
    assert len((await client.get("/api/stories?ignored=only")).json()) == 4
    assert (await client.post(f"/api/stories/{ids[1]}/unhide")).status_code == 204
    assert len((await client.get("/api/stories?ignored=only")).json()) == 3

    out = await client.post("/api/stories/unhide-all")
    assert out.status_code == 200 and out.json() == {"released": 3}
    assert len((await client.get("/api/stories")).json()) == 7
    assert (await client.post("/api/stories/unhide-all")).json() == {"released": 0}


async def test_unhide_all_keeps_rules_for_future_stories(
    client: AsyncClient, db_session, store
) -> None:
    ids, _ = await _muted(client, db_session, store)
    assert (await client.post("/api/stories/unhide-all")).json() == {"released": 5}
    assert len((await client.get("/api/topic-rules")).json()) == 1
    async with db_session() as s:
        story = Story(title="Fresh", summary="x", version=1, category="Tech")
        s.add(story)
        await s.flush()
        await store.upsert_story_centroid(story.id, _vec(0, jitter=0.1))
        await topics_svc.evaluate_story(s, story.id)
        await s.commit()
        fresh = story.id
    assert fresh not in {s["id"] for s in (await client.get("/api/stories")).json()}
    assert ids


async def test_set_aside_follow_and_saved_override(
    client: AsyncClient, db_session, store
) -> None:
    ids, rule_id = await _muted(client, db_session, store)
    await client.delete(f"/api/topic-rules/{rule_id}", params={"keep_hidden": "true"})
    assert len((await client.get("/api/stories")).json()) == 2
    await client.put(f"/api/stories/{ids[0]}/saved")
    assert ids[0] in {s["id"] for s in (await client.get("/api/stories")).json()}
    topic_id = (await client.get("/api/topics")).json()[0]["id"]
    await client.post(f"/api/topics/{topic_id}/rules", json={"mode": "follow"})
    assert len((await client.get("/api/stories")).json()) == 7
    assert (await client.get("/api/stories/counts")).json()["ignored"] == 0
