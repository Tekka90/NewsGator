"""Story presets: shared vectors, /api/me/presets, preset-aware story list and counts."""

import copy
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.api import stories as stories_api
from app.models import Article, Feed, Story, StoryState, UserFeed
from app.services import story_presets as sp

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "story_presets_vectors.json").read_text("utf-8")
)
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _eval_stories() -> list[sp.EvalStory]:
    out = []
    for s in VECTORS["dataset"]["stories"]:
        state = s["state"] or {"is_read": False, "read_at_version": 0, "saved_at": None}
        out.append(
            sp.EvalStory(
                id=s["id"], title=s["title"], summary=s["summary"], category=s["category"],
                version=s["version"], last_updated_at=_dt(s["last_updated_at"]),  # type: ignore[arg-type]
                article_feed_ids=tuple(a["feed_id"] for a in s["articles"]),
                article_published=tuple(_dt(a["published_at"]) for a in s["articles"]),
                article_is_video=tuple(a.get("media_kind") == "video" for a in s["articles"]),
                is_read=state["is_read"], read_at_version=state["read_at_version"],
                saved_at=_dt(state["saved_at"]),
            )
        )
    return out


def _criteria(partial: dict[str, Any]) -> dict[str, Any]:
    return {**sp.empty_criteria(), **partial}


def _custom(**kw: Any) -> dict[str, Any]:
    base = {
        "id": "c1", "kind": "custom", "label": "Mine", "icon": "star", "display": "icon+label",
        "enabled": True, "position": 4, "sort": "published", "order": "desc",
        "show_count": True, "criteria": sp.empty_criteria(),
    }
    return {**base, **kw}


# --- pure vectors ---------------------------------------------------------------------


def test_defaults_and_icon_catalog_match_vectors() -> None:
    assert sp.default_presets() == VECTORS["defaults"]
    assert list(sp.ICON_CATALOG) == VECTORS["icon_catalog"]
    assert len(VECTORS["icon_catalog"]) == 47
    by_id = {p["id"]: p for p in VECTORS["defaults"]}
    assert (by_id["unread"]["sort"], by_id["unread"]["order"]) == ("published", "asc")
    assert (by_id["updated"]["sort"], by_id["updated"]["order"]) == ("sources", "desc")
    assert (by_id["saved"]["sort"], by_id["saved"]["order"]) == ("saved_at", "desc")
    assert (by_id["all"]["sort"], by_id["all"]["order"]) == ("published", "desc")


def test_keyword_normalisation_vectors() -> None:
    for case in VECTORS["keyword_normalization"]["normalize"]:
        assert sp.normalize_text(case["input"]) == case["expected"], case
    for case in VECTORS["keyword_normalization"]["matches"]:
        assert sp.text_matches(case["title"], case["summary"], case["query"]) is case["expected"]


def test_validation_vectors() -> None:
    for case in VECTORS["validation"]["invalid"]:
        with pytest.raises(sp.PresetValidationError) as err:
            sp.validate_presets(copy.deepcopy(case["payload"]["presets"]))
        assert err.value.code == case["expect_code"], case["name"]
    for case in VECTORS["validation"]["valid"]:
        sp.validate_presets(copy.deepcopy(case["payload"]["presets"]))
    renum = VECTORS["validation"]["position_renumber"]
    result = sp.validate_presets(copy.deepcopy(renum["payload"]["presets"]))
    assert [p["id"] for p in result] == renum["expect_order"]
    assert [p["position"] for p in result] == list(range(len(result)))


def test_icon_validity_vectors() -> None:
    for case in VECTORS["icon_validity"]:
        assert sp.is_valid_icon(case["icon"]) is case["valid"], case


def _feed_kinds() -> dict[int, str]:
    return {
        f["id"]: sp.effective_source_kind(f["kind"], f["url"])
        for f in VECTORS["dataset"]["feeds"]
    }


def test_reference_evaluator_matches_criteria_and_sort_vectors() -> None:
    stories = _eval_stories()
    kinds = _feed_kinds()
    for case in VECTORS["criteria_matrix"]:
        hits = [s for s in stories if sp.story_matches(s, _criteria(case["criteria"]), kinds, NOW)]
        got = [s.id for s in sp.sort_stories(hits, case["sort"], case["order"])]
        assert got == case["expect_ids"], case["name"]
    for case in VECTORS["sort_vectors"]:
        got = [s.id for s in sp.sort_stories(stories, case["sort"], case["order"])]
        assert got == case["expect_ids"], case


# --- API fixtures ---------------------------------------------------------------------


async def _load_dataset(db_session, user_ids: tuple[int, ...] = (1,)) -> None:
    async with db_session() as s:
        for f in VECTORS["dataset"]["feeds"]:
            s.add(Feed(id=f["id"], kind=f["kind"], url=f["url"], title=f"F{f['id']}"))
        await s.flush()
        for uid in user_ids:
            s.add_all(UserFeed(user_id=uid, feed_id=f["id"]) for f in VECTORS["dataset"]["feeds"])
        for st in VECTORS["dataset"]["stories"]:
            s.add(
                Story(
                    id=st["id"], title=st["title"], summary=st["summary"],
                    category=st["category"], version=st["version"],
                    last_updated_at=_dt(st["last_updated_at"]),
                )
            )
            await s.flush()
            for i, a in enumerate(st["articles"]):
                s.add(
                    Article(
                        feed_id=a["feed_id"], guid=f"s{st['id']}-{i}",
                        url=f"https://x.example/{st['id']}/{i}", title="a",
                        story_id=st["id"], published_at=_dt(a["published_at"]),
                        processing_state="clustered", media_kind=a.get("media_kind"),
                    )
                )
            state = st["state"]
            if state:
                s.add(
                    StoryState(
                        user_id=1, story_id=st["id"], is_read=state["is_read"],
                        read_at_version=state["read_at_version"],
                        saved_at=_dt(state["saved_at"]),
                    )
                )
        await s.commit()


@pytest.fixture()
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stories_api, "utcnow", lambda: NOW)


async def _put(client: AsyncClient, presets: list[dict[str, Any]], **extra: Any):
    return await client.put("/api/me/presets", json={"presets": presets, **extra})


def _ids(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [s["id"] for s in response.json()]


# --- vectors through the HTTP API (SQL implementation) ---------------------------------


async def test_default_presets_order_lists_and_counts(
    client: AsyncClient, db_session, fixed_now
) -> None:
    await setup_admin(client)
    await _load_dataset(db_session)
    expected = VECTORS["default_preset_results"]
    # Unread is oldest first; Updated is most sources first; Saved is most recently saved first
    assert _ids(await client.get("/api/stories?preset=unread")) == [5, 6, 1] == expected["unread"]
    assert _ids(await client.get("/api/stories?preset=updated")) == [2, 3] == expected["updated"]
    assert _ids(await client.get("/api/stories?preset=saved")) == [6, 4, 3] == expected["saved"]
    assert _ids(await client.get("/api/stories?preset=all")) == expected["all"]
    counts = (await client.get("/api/stories/preset-counts")).json()["counts"]
    assert counts == {k: len(v) for k, v in expected.items()}


async def test_criteria_matrix_via_list_and_counts(
    client: AsyncClient, db_session, fixed_now
) -> None:
    await setup_admin(client)
    await _load_dataset(db_session)
    for case in VECTORS["criteria_matrix"]:
        preset = _custom(
            criteria=_criteria(case["criteria"]), sort=case["sort"], order=case["order"]
        )
        saved = await _put(client, [*sp.default_presets(), preset])
        assert saved.status_code == 200, (case["name"], saved.text)
        listed = _ids(await client.get("/api/stories?preset=c1"))
        assert listed == case["expect_ids"], case["name"]
        counts = (await client.get("/api/stories/preset-counts")).json()["counts"]
        assert counts["c1"] == len(case["expect_ids"]), case["name"]


async def test_sort_vectors_via_explicit_params(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    await _load_dataset(db_session)
    for case in VECTORS["sort_vectors"]:
        url = f"/api/stories?sort={case['sort']}&order={case['order']}"
        assert _ids(await client.get(url)) == case["expect_ids"], case


# --- /api/me/presets -------------------------------------------------------------------


async def test_get_defaults_not_persisted_and_roundtrip(client: AsyncClient) -> None:
    await setup_admin(client)
    body = (await client.get("/api/me/presets")).json()
    assert body == {"presets": VECTORS["defaults"], "last_preset_id": None, "compact": False}

    presets = sp.default_presets()
    presets[0]["label"] = "Everything"
    for preset in presets:
        preset["position"] += 1
    presets.append(_custom(position=0, criteria=_criteria({"query": "  garage  "})))
    r = await _put(client, presets, last_preset_id="c1", compact=True)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [p["id"] for p in out["presets"]] == ["c1", "all", "unread", "updated", "saved"]
    assert [p["position"] for p in out["presets"]] == [0, 1, 2, 3, 4]
    assert out["presets"][0]["criteria"]["query"] == "garage"
    assert out["last_preset_id"] == "c1" and out["compact"] is True
    assert (await client.get("/api/me/presets")).json() == out
    me = (await client.get("/api/auth/me")).json()
    assert me["last_preset_id"] == "c1"


async def test_put_rejects_invalid_with_stable_codes(client: AsyncClient) -> None:
    await setup_admin(client)
    for case in VECTORS["validation"]["invalid"]:
        r = await client.put("/api/me/presets", json=case["payload"])
        assert r.status_code == 422, case["name"]
        assert r.json()["detail"]["code"] == case["expect_code"], case["name"]
    for bad in ({}, {"presets": "x"}, {"presets": sp.default_presets(), "nope": 1},
                {"presets": sp.default_presets(), "compact": "yes"}):
        r = await client.put("/api/me/presets", json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_payload"
    too_many = sp.default_presets() + [_custom(id=f"c{i}", position=4 + i) for i in range(200)]
    r = await _put(client, too_many)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_presets"


async def test_unknown_last_preset_id_collapses_to_null(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await _put(client, sp.default_presets(), last_preset_id="gone")
    assert r.json()["last_preset_id"] is None


async def test_reset_scopes(client: AsyncClient) -> None:
    await setup_admin(client)
    presets = sp.default_presets()
    presets[1].update(label="Mine", icon="star", sort="updated", order="desc", enabled=False)
    presets.append(_custom())
    await _put(client, presets, last_preset_id="c1", compact=True)

    r = await client.post("/api/me/presets/reset", json={"scope": "builtin"})
    out = r.json()
    assert [p["id"] for p in out["presets"]] == ["all", "unread", "updated", "saved", "c1"]
    assert out["presets"][1] == {**VECTORS["defaults"][1]}
    assert out["last_preset_id"] == "c1" and out["compact"] is True

    r = await client.post("/api/me/presets/reset", json={"scope": "all"})
    assert r.json() == {"presets": VECTORS["defaults"], "last_preset_id": None, "compact": False}

    for bad in ({}, {"scope": "x"}, {"scope": "all", "more": 1}):
        r = await client.post("/api/me/presets/reset", json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_scope"


async def test_patch_me_last_preset_id(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.patch("/api/auth/me", json={"last_preset_id": "updated"})
    assert r.status_code == 200 and r.json()["last_preset_id"] == "updated"
    r = await client.patch("/api/auth/me", json={"last_preset_id": "nope"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_last_preset_id"
    r = await client.patch("/api/auth/me", json={"last_preset_id": ""})
    assert r.json()["last_preset_id"] is None


async def test_presets_are_per_user_and_hidden_feeds_are_dropped(
    client: AsyncClient, db_session
) -> None:
    await setup_admin(client)
    await _load_dataset(db_session)
    r = await client.post(
        "/api/users", json={"username": "reader", "password": "readerpass1", "is_admin": False}
    )
    assert r.status_code == 201
    # admin's custom preset references feed 1 and feed 99 (unknown) and feed 3
    async with db_session() as s:
        s.add(Feed(id=99, kind="rss", url="https://hidden.example/rss", title="hidden"))
        await s.commit()
    mine = _custom(criteria=_criteria({"feed_ids": [1, 99]}))
    await _put(client, [*sp.default_presets(), mine])
    out = (await client.get("/api/me/presets")).json()
    assert out["presets"][-1]["criteria"]["feed_ids"] == [1]  # 99 is not subscribed → dropped

    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "reader", "password": "readerpass1"})
    other = (await client.get("/api/me/presets")).json()
    assert other == {"presets": VECTORS["defaults"], "last_preset_id": None, "compact": False}
    assert (await client.get("/api/stories?preset=c1")).status_code == 404
    assert (await client.get("/api/stories/preset-counts")).json()["counts"] == {
        k: 0 for k in VECTORS["default_preset_results"]
    }


# --- /api/stories extensions -----------------------------------------------------------


async def test_unknown_or_disabled_preset_is_404(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    await _load_dataset(db_session)
    assert (await client.get("/api/stories?preset=nope")).status_code == 404
    presets = sp.default_presets()
    presets[2]["enabled"] = False
    await _put(client, presets)
    r = await client.get("/api/stories?preset=updated")
    assert r.status_code == 404 and "detail" in r.json()
    counts = (await client.get("/api/stories/preset-counts")).json()["counts"]
    assert "updated" not in counts


async def test_preset_overrides_and_manual_filters_layer_on_top(
    client: AsyncClient, db_session, fixed_now
) -> None:
    await setup_admin(client)
    await _load_dataset(db_session)
    # explicit sort/order replace the preset's
    assert _ids(await client.get("/api/stories?preset=unread&order=desc")) == [1, 6, 5]
    assert _ids(await client.get("/api/stories?preset=all&sort=sources&order=desc"))[:2] == [4, 2]
    # feed / feed_ids / category AND with the preset
    assert _ids(await client.get("/api/stories?preset=unread&feed=5")) == [6]
    assert _ids(await client.get("/api/stories?preset=all&feed_ids=1&feed_ids=5")) == [4, 1, 2, 6]
    assert _ids(await client.get("/api/stories?preset=unread&category=Tech")) == [6, 1]
    assert _ids(await client.get("/api/stories?feed_ids=999")) == []
    # nothing about a preset is stored by merely listing
    assert (await client.get("/api/me/presets")).json()["presets"] == VECTORS["defaults"]


async def test_legacy_params_unchanged_without_preset(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    await _load_dataset(db_session)
    assert _ids(await client.get("/api/stories")) == [5, 6, 3, 2, 1, 4]  # published asc
    assert _ids(await client.get("/api/stories?filter=unread")) == [5, 6, 1]
    assert _ids(await client.get("/api/stories?saved=true&sort=updated&order=desc")) == [4, 3, 6]
    assert _ids(await client.get("/api/stories?feed=1&sort=sources&order=desc"))[0] == 4
    assert (await client.get("/api/stories?sort=bogus")).status_code == 422


async def test_preset_counts_use_bounded_queries(
    client: AsyncClient, db_session, fixed_now
) -> None:
    from sqlalchemy import event

    from app.core import db

    await setup_admin(client)
    await _load_dataset(db_session)
    await _put(client, [*sp.default_presets(), _custom(criteria=_criteria({"feed_ids": [1]}))])
    seen: list[str] = []
    event.listen(
        db.get_engine().sync_engine, "before_cursor_execute",
        lambda conn, cur, stmt, *a: seen.append(stmt),
    )
    counts = (await client.get("/api/stories/preset-counts")).json()["counts"]
    assert counts["c1"] == 3
    selects = [s for s in seen if s.lstrip().upper().startswith("SELECT")]
    # user + feeds + presets, one aggregate for the four status presets, one for the custom
    assert len(selects) <= 5, selects


# --- migration ---------------------------------------------------------------------------


def _alembic(db_file: Path, *args: str) -> None:
    backend = Path(__file__).resolve().parents[1]
    env = {"DATABASE_URL": f"sqlite+aiosqlite:///{db_file}", "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=backend, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _user_columns(db_file: Path) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(user)")}
    finally:
        conn.close()


def test_migration_0024_up_and_down_keeps_existing_users(tmp_path: Path) -> None:
    db_file = tmp_path / "mig.db"
    _alembic(db_file, "upgrade", "0023_article_story_dates_index")
    new = {"story_presets", "last_preset_id", "presets_compact"}
    assert not new & _user_columns(db_file)
    conn = sqlite3.connect(db_file)
    conn.execute(
        'INSERT INTO "user" (username, password_hash, is_admin, summary_language, story_sort, '
        "story_order, story_filter, created_at) "
        "VALUES ('u', 'h', 1, '', 'published', 'asc', 'unread', '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    _alembic(db_file, "upgrade", "head")
    assert new <= _user_columns(db_file)
    conn = sqlite3.connect(db_file)
    row = conn.execute(
        'SELECT story_presets, last_preset_id, presets_compact, story_filter FROM "user"'
    ).fetchone()
    conn.close()
    assert row == (None, None, 0, "unread")

    _alembic(db_file, "downgrade", "0023_article_story_dates_index")
    assert not new & _user_columns(db_file)
    conn = sqlite3.connect(db_file)
    assert conn.execute('SELECT story_filter FROM "user"').fetchone() == ("unread",)
    conn.close()


# --- video source kind: persisted media_kind flag --------------------------------------

LINKS = VECTORS["video_flag_vectors"]


def _link_stories() -> list[sp.EvalStory]:
    out = []
    for s in LINKS["dataset"]["stories"]:
        state = s["state"] or {"is_read": False, "read_at_version": 0, "saved_at": None}
        out.append(
            sp.EvalStory(
                id=s["id"], title=s["title"], summary=s["summary"], category=s["category"],
                version=s["version"], last_updated_at=_dt(s["last_updated_at"]),  # type: ignore[arg-type]
                article_feed_ids=tuple(a["feed_id"] for a in s["articles"]),
                article_published=tuple(_dt(a["published_at"]) for a in s["articles"]),
                article_is_video=tuple(a.get("media_kind") == "video" for a in s["articles"]),
                is_read=state["is_read"], read_at_version=state["read_at_version"],
                saved_at=_dt(state["saved_at"]),
            )
        )
    return out


def test_video_flag_vectors_reference() -> None:
    kinds = {
        f["id"]: sp.effective_source_kind(f["kind"], f["url"]) for f in LINKS["dataset"]["feeds"]
    }
    stories = _link_stories()
    for case in LINKS["cases"]:
        hits = [s for s in stories if sp.story_matches(s, _criteria(case["criteria"]), kinds, NOW)]
        assert sorted(s.id for s in hits) == case["expect_ids"], case["name"]


async def test_video_flag_vectors_via_sql_list_and_counts(
    client: AsyncClient, db_session, fixed_now
) -> None:
    await setup_admin(client)
    async with db_session() as s:
        for f in LINKS["dataset"]["feeds"]:
            s.add(Feed(id=f["id"], kind=f["kind"], url=f["url"], title=f"F{f['id']}"))
        await s.flush()
        for f in LINKS["dataset"]["feeds"]:
            s.add(UserFeed(user_id=1, feed_id=f["id"]))
        for st in LINKS["dataset"]["stories"]:
            s.add(
                Story(
                    id=st["id"], title=st["title"], summary=st["summary"], category=st["category"],
                    version=st["version"], last_updated_at=_dt(st["last_updated_at"]),
                )
            )
            await s.flush()
            for i, a in enumerate(st["articles"]):
                s.add(
                    Article(
                        feed_id=a["feed_id"], guid=f"l{st['id']}-{i}", url=a["url"], title="a",
                        story_id=st["id"], processing_state="clustered",
                        media_kind=a.get("media_kind"),
                    )
                )
            if st["state"]:
                s.add(
                    StoryState(
                        user_id=1, story_id=st["id"], is_read=st["state"]["is_read"],
                        read_at_version=st["state"]["read_at_version"],
                        saved_at=_dt(st["state"]["saved_at"]),
                    )
                )
        await s.commit()
    for case in LINKS["cases"]:
        preset = _custom(criteria=_criteria(case["criteria"]), sort="updated", order="asc")
        assert (await _put(client, [*sp.default_presets(), preset])).status_code == 200
        assert _ids(await client.get("/api/stories?preset=c1")) == case["expect_ids"], case["name"]
        counts = (await client.get("/api/stories/preset-counts")).json()["counts"]
        assert counts["c1"] == len(case["expect_ids"]), case["name"]


def test_migration_0025_flags_stories_of_shared_links(tmp_path: Path) -> None:
    db_file = tmp_path / "mig25.db"
    _alembic(db_file, "upgrade", "0024_story_presets")
    conn = sqlite3.connect(db_file)
    for uid in (1, 2):
        conn.execute(
            'INSERT INTO "user" (id, username, password_hash, is_admin, summary_language, '
            "story_sort, story_order, story_filter, presets_compact, created_at) "
            f"VALUES ({uid}, 'u{uid}', 'h', 0, '', 'published', 'asc', 'unread', 0, '2026-01-01')"
        )
    for fid, url, kind in ((1, "saved:1", "saved"), (2, "https://a.example/rss", "rss")):
        conn.execute(
            "INSERT INTO feed (id, url, kind, title, created_at) "
            f"VALUES ({fid}, '{url}', '{kind}', 'F', '2026-01-01')"
        )
    for sid in (1, 2, 3):
        conn.execute(
            "INSERT INTO story (id, title, summary, category, language, version, is_frozen, "
            f"first_seen_at, last_updated_at) VALUES ({sid}, 't', 's', 'c', 'en', 1, 0, "
            "'2026-01-01', '2026-01-01')"
        )
    for aid, fid, sid in ((1, 1, 1), (2, 1, 2), (3, 2, 3)):
        conn.execute(
            "INSERT INTO article (id, feed_id, guid, url, title, raw_content, language, story_id, "
            "processing_state, content_status, fetched_at) "
            f"VALUES ({aid}, {fid}, 'g{aid}', 'https://x/{aid}', 't', '', 'en', {sid}, "
            "'clustered', 'ok', '2026-01-02 00:00:00')"
        )
    conn.execute(
        "INSERT INTO story_state (user_id, story_id, is_read, read_at_version) VALUES (1, 2, 1, 1)"
    )
    conn.commit()
    conn.close()

    _alembic(db_file, "upgrade", "head")
    conn = sqlite3.connect(db_file)
    rows = conn.execute(
        "SELECT user_id, story_id, is_read, read_at_version, saved_at IS NOT NULL "
        "FROM story_state ORDER BY story_id"
    ).fetchall()
    conn.close()
    # story 1 gets an unread saved-only row; story 2 keeps its read state; story 3 (RSS) untouched
    assert rows == [(1, 1, 0, 0, 1), (1, 2, 1, 1, 1)]


def test_youtube_source_kind_is_removed() -> None:
    assert "youtube" not in sp.SOURCE_KINDS and "video" in sp.SOURCE_KINDS
    payload = [*sp.default_presets(), _custom(criteria=_criteria({"source_kinds": ["youtube"]}))]
    with pytest.raises(sp.PresetValidationError) as err:
        sp.validate_presets(payload)
    assert err.value.code == "invalid_enum"


async def test_put_rejects_youtube_kind_and_accepts_video(client: AsyncClient) -> None:
    await setup_admin(client)
    rejected = _custom(criteria=_criteria({"source_kinds": ["youtube"]}))
    assert (await _put(client, [*sp.default_presets(), rejected])).status_code == 422
    accepted = _custom(criteria=_criteria({"source_kinds": ["video"]}))
    assert (await _put(client, [*sp.default_presets(), accepted])).status_code == 200


def test_effective_source_kind_still_describes_channel_feeds() -> None:
    channel = "https://www.youtube.com/feeds/videos.xml?channel_id=UC" + "a" * 22
    assert sp.effective_source_kind("rss", channel) == "youtube"
    assert sp.effective_source_kind("rss", "https://a.example/rss") == "rss"
    assert sp.effective_source_kind("saved", "saved:1") == "saved"


def test_no_leftover_youtube_preset_predicate() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "app"
    sources = "".join(p.read_text("utf-8") for p in root.rglob("*.py"))
    assert "ng_youtube_video" not in sources
    assert "is_youtube_video_link" not in sources
    assert "_stories_with_youtube_video" not in sources


def _migration_db(tmp_path: Path, name: str, upto: str) -> Path:
    db_file = tmp_path / name
    _alembic(db_file, "upgrade", upto)
    return db_file


def _insert_basics(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO feed (id, url, kind, title, created_at) "
        "VALUES (1, 'https://a.example/rss', 'rss', 'F', '2026-01-01')"
    )
    rows = (
        (1, "https://youtu.be/dQw4w9WgXcQ?si=x"),
        (2, "https://www.youtube.com/shorts/aaaaaaaaaaa"),
        (3, "https://notyoutube.com/watch?v=dQw4w9WgXcQ"),
        (4, "https://example.com/youtube-article"),
    )
    for aid, url in rows:
        conn.execute(
            "INSERT INTO article (id, feed_id, guid, url, title, raw_content, language, "
            "processing_state, content_status, fetched_at) "
            f"VALUES ({aid}, 1, 'g{aid}', '{url}', 't', '', 'en', 'clustered', 'ok', "
            "'2026-01-02 00:00:00')"
        )


def test_migration_0026_backfills_video_flag_and_downgrades(tmp_path: Path) -> None:
    db_file = _migration_db(tmp_path, "mig26.db", "0025_saved_shared_links")
    conn = sqlite3.connect(db_file)
    _insert_basics(conn)
    conn.commit()
    conn.close()

    _alembic(db_file, "upgrade", "0026_article_video_flag")
    conn = sqlite3.connect(db_file)
    rows = conn.execute(
        "SELECT id, media_kind, video_provider, video_id, video_url, video_embed_url, "
        "video_duration FROM article ORDER BY id"
    ).fetchall()
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(article)")}
    conn.close()
    assert rows == [
        (1, "video", "youtube", "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
         "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", None),
        (2, "video", "youtube", "aaaaaaaaaaa", "https://www.youtube.com/watch?v=aaaaaaaaaaa",
         "https://www.youtube-nocookie.com/embed/aaaaaaaaaaa", None),
        (3, None, None, None, None, None, None),
        (4, None, None, None, None, None, None),
    ]
    assert "ix_article_media_kind" in indexes

    _alembic(db_file, "downgrade", "0025_saved_shared_links")
    conn = sqlite3.connect(db_file)
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(article)")}
    conn.close()
    assert not columns & {"media_kind", "video_url", "video_embed_url", "video_provider",
                          "video_id", "video_duration"}


def _kinds(conn: sqlite3.Connection, uid: int) -> list[list[str]]:
    stored = conn.execute('SELECT story_presets FROM "user" WHERE id = ?', (uid,)).fetchone()[0]
    return [
        p["criteria"]["source_kinds"]
        for p in json.loads(stored)
        if isinstance(p, dict) and isinstance(p.get("criteria"), dict)
    ]


def test_migration_0027_rewrites_youtube_presets_to_video(tmp_path: Path) -> None:
    db_file = _migration_db(tmp_path, "mig27.db", "0026_article_video_flag")
    conn = sqlite3.connect(db_file)

    def preset(pid: str, kinds: list[str]) -> dict[str, Any]:
        return {"id": pid, "criteria": {"source_kinds": kinds}}

    documents = {
        1: [preset("a", ["youtube"]), preset("b", ["rss", "youtube", "video"]),
            preset("c", ["mail"])],
        2: None,
        3: "not json",
        4: {"not": "a list"},
        5: [preset("d", ["video", "youtube"]), "junk", {"id": "e", "criteria": None}],
    }
    for uid, doc in documents.items():
        stored = doc if doc is None or isinstance(doc, str) else json.dumps(doc)
        conn.execute(
            'INSERT INTO "user" (id, username, password_hash, is_admin, summary_language, '
            "story_sort, story_order, story_filter, presets_compact, created_at, story_presets) "
            f"VALUES ({uid}, 'u{uid}', 'h', 0, '', 'published', 'asc', 'unread', 0, "
            "'2026-01-01', ?)",
            (stored,),
        )
    conn.commit()
    conn.close()

    _alembic(db_file, "upgrade", "0027_presets_video_kind")
    conn = sqlite3.connect(db_file)
    assert _kinds(conn, 1) == [["video"], ["rss", "video"], ["mail"]]
    assert conn.execute('SELECT story_presets FROM "user" WHERE id = 2').fetchone()[0] is None
    raw3 = conn.execute('SELECT story_presets FROM "user" WHERE id = 3').fetchone()[0]
    assert raw3 == "not json"
    raw4 = conn.execute('SELECT story_presets FROM "user" WHERE id = 4').fetchone()[0]
    assert json.loads(raw4) == {"not": "a list"}
    assert _kinds(conn, 5)[0] == ["video"]
    conn.close()

    _alembic(db_file, "downgrade", "0026_article_video_flag")
    conn = sqlite3.connect(db_file)
    assert _kinds(conn, 1) == [["youtube"], ["rss", "youtube"], ["mail"]]
    conn.close()
