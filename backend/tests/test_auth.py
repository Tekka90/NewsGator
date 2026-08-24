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
