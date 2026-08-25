"""Auth flow tests (Milestone 1 acceptance)."""

from httpx import AsyncClient
from tests.conftest import ADMIN, setup_admin


async def test_setup_creates_admin_and_session(client: AsyncClient) -> None:
    r = await client.get("/api/auth/setup-needed")
    assert r.json() == {"setup_needed": True}

    r = await client.post("/api/auth/setup", json=ADMIN)
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "admin"
    assert body["is_admin"] is True

    # Session cookie works
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


async def test_setup_rejected_twice(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.post(
        "/api/auth/setup", json={"username": "second", "password": "password9"}
    )
    assert r.status_code == 409


async def test_login_logout(client: AsyncClient) -> None:
    await setup_admin(client)
    await client.post("/api/auth/logout")
    r = await client.get("/api/auth/me")
    assert r.status_code == 401

    r = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong-password"}
    )
    assert r.status_code == 401

    r = await client.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_patch_me_language(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.patch("/api/auth/me", json={"summary_language": "fr"})
    assert r.status_code == 200
    assert r.json()["summary_language"] == "fr"


async def test_login_returns_portable_token(client: AsyncClient) -> None:
    """iOS standalone PWAs may drop cookies — the token in the body must work
    as a Bearer credential (and as ?token= for SSE)."""
    await setup_admin(client)
    await client.post("/api/auth/logout")
    client.cookies.clear()

    r = await client.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 200
    token = r.json()["token"]
    assert token
    client.cookies.clear()  # exercise token-only auth paths

    r = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["username"] == "admin"

    r = await client.get("/api/auth/me", params={"token": token})
    assert r.status_code == 200

    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


async def test_patch_me_story_ordering_prefs(client: AsyncClient) -> None:
    await setup_admin(client)
    r = await client.get("/api/auth/me")
    assert r.json()["story_sort"] == ""  # unset → server default
    assert r.json()["story_filter"] == ""

    r = await client.patch(
        "/api/auth/me",
        json={"story_sort": "published", "story_order": "asc", "story_filter": "updated"},
    )
    assert r.status_code == 200
    assert r.json()["story_sort"] == "published"
    assert r.json()["story_order"] == "asc"
    assert r.json()["story_filter"] == "updated"

    assert (await client.get("/api/auth/me")).json()["story_filter"] == "updated"
    assert (
        await client.patch("/api/auth/me", json={"story_sort": "bogus"})
    ).status_code == 422
    assert (
        await client.patch("/api/auth/me", json={"story_filter": "bogus"})
    ).status_code == 422
