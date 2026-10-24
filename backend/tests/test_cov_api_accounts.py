"""Auth (/auth), user admin (/users) and category taxonomy (/categories) edge cases."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import ADMIN, setup_admin

from app.core.security import SESSION_COOKIE
from app.models import Category, Feed, User, UserFeed
from app.services import story_presets, translation

READER = {"username": "reader", "password": "readerpass1"}


async def _become_reader(client: AsyncClient) -> int:
    r = await client.post("/api/users", json=READER)
    assert r.status_code == 201
    await client.post("/api/auth/logout")
    assert (await client.post("/api/auth/login", json=READER)).status_code == 200
    return int(r.json()["id"])


# --- auth ---


async def test_setup_needed_flips_and_second_setup_is_refused(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/setup-needed")).json() == {"setup_needed": True}
    await setup_admin(client)
    assert (await client.get("/api/auth/setup-needed")).json() == {"setup_needed": False}
    r = await client.post("/api/auth/setup", json={"username": "other", "password": "password99"})
    assert r.status_code == 409


async def test_setup_validates_input(client: AsyncClient) -> None:
    r = await client.post("/api/auth/setup", json={"username": "ab", "password": "password99"})
    assert r.status_code == 422
    r = await client.post("/api/auth/setup", json={"username": "admin", "password": "short"})
    assert r.status_code == 422


async def test_setup_subscribes_admin_to_preexisting_feeds(
    client: AsyncClient, db_session
) -> None:
    async with db_session() as s:
        s.add_all([Feed(url="https://a.example.com/rss"), Feed(url="https://b.example.com/rss")])
        await s.commit()
    await setup_admin(client)
    async with db_session() as s:
        assert len((await s.scalars(select(UserFeed))).all()) == 2
    assert len((await client.get("/api/feeds")).json()) == 2


async def test_login_rejects_unknown_user_and_bad_password(client: AsyncClient) -> None:
    await setup_admin(client)
    bad_pw = await client.post("/api/auth/login", json={**ADMIN, "password": "wrongwrong"})
    unknown = await client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()  # no user-enumeration signal


async def test_session_token_authenticates_via_bearer_and_query(client: AsyncClient) -> None:
    await setup_admin(client)
    token = (await client.post("/api/auth/session-token")).json()["token"]
    await client.post("/api/auth/logout")
    assert (await client.get("/api/auth/me")).status_code == 401

    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["username"] == "admin"
    assert (await client.get("/api/auth/me", params={"token": token})).status_code == 200
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


async def test_session_token_requires_login(client: AsyncClient) -> None:
    assert (await client.post("/api/auth/session-token")).status_code == 401


async def test_session_debug_reports_credential_kind(client: AsyncClient) -> None:
    await setup_admin(client)
    token = (await client.post("/api/auth/session-token")).json()["token"]
    client.cookies.clear()

    assert (await client.get("/api/auth/session-debug")).json() == {
        "presented": "none", "token_valid": False,
    }
    r = await client.get("/api/auth/session-debug", headers={"authorization": f"bearer {token}"})
    assert r.json() == {"presented": "bearer", "token_valid": True}
    r = await client.get("/api/auth/session-debug", headers={"authorization": "Bearer nope"})
    assert r.json() == {"presented": "bearer", "token_valid": False}
    r = await client.get("/api/auth/session-debug", params={"token": token})
    assert r.json() == {"presented": "query", "token_valid": True}
    client.cookies.set(SESSION_COOKIE, token)
    body = (await client.get("/api/auth/session-debug")).json()
    assert body["presented"] == "cookie"


async def test_patch_me_preferences_and_password(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.patch(
        "/api/auth/me",
        json={"story_sort": "sources", "story_order": "desc", "story_filter": "updated"},
    )
    assert r.status_code == 200
    me = (await client.get("/api/auth/me")).json()
    assert (me["story_sort"], me["story_order"], me["story_filter"]) == (
        "sources", "desc", "updated",
    )
    # Invalid enum values are rejected and leave the stored values alone
    assert (await client.patch("/api/auth/me", json={"story_sort": "random"})).status_code == 422
    assert (await client.patch("/api/auth/me", json={"password": "short"})).status_code == 422
    assert (await client.get("/api/auth/me")).json()["story_sort"] == "sources"

    # Web interface language: "" (auto) and the 10 supported codes only
    assert (await client.get("/api/auth/me")).json()["ui_language"] == ""
    assert (await client.patch("/api/auth/me", json={"ui_language": "zh-Hant"})).status_code == 200
    assert (await client.get("/api/auth/me")).json()["ui_language"] == "zh-Hant"
    assert (await client.patch("/api/auth/me", json={"ui_language": "xx"})).status_code == 422
    assert (await client.patch("/api/auth/me", json={"ui_language": ""})).status_code == 200
    assert (await client.get("/api/auth/me")).json()["ui_language"] == ""

    r = await client.patch("/api/auth/me", json={"password": "brandnewpass1"})
    assert r.status_code == 200
    await client.post("/api/auth/logout")
    assert (await client.post("/api/auth/login", json=ADMIN)).status_code == 401
    r = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "brandnewpass1"}
    )
    assert r.status_code == 200


async def test_patch_me_summary_language_prewarms_translations(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    calls: list[tuple[int, str]] = []

    async def fake_prewarm(session: Any, user_id: int, lang: str, limit: int = 50) -> None:
        calls.append((user_id, lang))

    monkeypatch.setattr(translation, "prewarm_user_stories", fake_prewarm)
    r = await client.patch("/api/auth/me", json={"summary_language": "fr"})
    assert r.status_code == 200 and r.json()["summary_language"] == "fr"
    assert calls == [(1, "fr")]

    # Empty string reverts to the server default without prewarming
    r = await client.patch("/api/auth/me", json={"summary_language": ""})
    assert r.json()["summary_language"] in ("", None)
    assert calls == [(1, "fr")]


async def test_patch_me_last_preset_validation(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.patch("/api/auth/me", json={"last_preset_id": "does-not-exist"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_last_preset_id"

    preset_id = story_presets.default_presets()[0]["id"]
    r = await client.patch("/api/auth/me", json={"last_preset_id": preset_id})
    assert r.status_code == 200
    assert r.json()["last_preset_id"] == preset_id
    # "" clears it
    r = await client.patch("/api/auth/me", json={"last_preset_id": ""})
    assert r.json()["last_preset_id"] is None


# --- users ---


async def test_user_update_promote_and_not_found(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    user_id = (await client.post("/api/users", json=READER)).json()["id"]

    r = await client.patch(f"/api/users/{user_id}", json={"is_admin": True})
    assert r.json()["is_admin"] is True
    async with db_session() as s:
        assert (await s.get(User, user_id)).is_admin is True  # type: ignore[union-attr]
    # Demotion is fine while another admin remains
    r = await client.patch(f"/api/users/{user_id}", json={"is_admin": False})
    assert r.json()["is_admin"] is False

    assert (await client.patch("/api/users/999", json={"is_admin": True})).status_code == 404
    assert (await client.delete("/api/users/999")).status_code == 404


async def test_user_create_validation(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post("/api/users", json={"username": "x", "password": "password99"})
    assert r.status_code == 422


# --- categories ---


async def test_categories_require_admin(client: AsyncClient) -> None:
    await setup_admin(client)
    await _become_reader(client)
    assert (await client.get("/api/categories")).status_code == 403
    assert (await client.post("/api/categories", json={"name": "X"})).status_code == 403
    assert (await client.get("/api/categories/suggestions")).status_code == 403


async def test_category_rename_delete_and_guards(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    async with db_session() as s:
        existing = (await s.scalars(select(Category.name))).all()
    name = "Zzz Brand New"
    assert name not in existing

    cid = (await client.post("/api/categories", json={"name": name})).json()["id"]
    assert (await client.post("/api/categories", json={"name": name})).status_code == 409

    r = await client.patch(f"/api/categories/{cid}", json={"name": "Renamed"})
    assert r.json() == {"id": cid, "name": "Renamed"}
    assert (await client.patch("/api/categories/9999", json={"name": "x"})).status_code == 404

    assert (await client.delete(f"/api/categories/{cid}")).status_code == 204
    assert "Renamed" not in [c["name"] for c in (await client.get("/api/categories")).json()]
    assert (await client.delete(f"/api/categories/{cid}")).status_code == 404


async def test_uncategorized_cannot_be_deleted(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    async with db_session() as s:
        cat = await s.scalar(select(Category).where(Category.name == "Uncategorized"))
        if cat is None:
            cat = Category(name="Uncategorized")
            s.add(cat)
            await s.commit()
        cat_id = cat.id
    r = await client.delete(f"/api/categories/{cat_id}")
    assert r.status_code == 400
    assert "Uncategorized" in r.json()["detail"]


async def test_accept_suggestion_validation(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post(
        "/api/categories/suggestions/accept", json={"normalized_text": "x", "label": "   "}
    )
    assert r.status_code == 400

    await client.post("/api/categories", json={"name": "Taken"})
    r = await client.post(
        "/api/categories/suggestions/accept",
        json={"normalized_text": "taken", "label": "  Taken "},
    )
    assert r.status_code == 409
