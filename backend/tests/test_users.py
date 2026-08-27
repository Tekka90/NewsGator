"""Admin user-management tests (/api/users)."""

from httpx import AsyncClient
from sqlalchemy import func, select
from tests.conftest import setup_admin

from app.models import Story, StoryState, User

USER = {"username": "reader", "password": "readerpass1"}


async def _create_user(
    client: AsyncClient, username: str = "reader", password: str = "readerpass1",
    is_admin: bool = False,
) -> dict:
    r = await client.post(
        "/api/users",
        json={"username": username, "password": password, "is_admin": is_admin},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_users_require_admin(client: AsyncClient) -> None:
    # Anonymous
    assert (await client.get("/api/users")).status_code == 401

    await setup_admin(client)
    await _create_user(client)
    # Log in as the non-admin user
    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json=USER)
    assert r.status_code == 200

    assert (await client.get("/api/users")).status_code == 403
    assert (await client.post("/api/users", json=USER)).status_code == 403
    assert (await client.patch("/api/users/1", json={"is_admin": True})).status_code == 403
    assert (await client.delete("/api/users/1")).status_code == 403


async def test_create_list_and_login(client: AsyncClient) -> None:
    await setup_admin(client)
    created = await _create_user(client)
    assert created["username"] == "reader"
    assert created["is_admin"] is False
    assert created["created_at"]

    r = await client.get("/api/users")
    assert [u["username"] for u in r.json()] == ["admin", "reader"]
    # Password hashes are never exposed
    assert "password" not in r.text and "password_hash" not in r.text

    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json=USER)
    assert r.status_code == 200
    assert r.json()["is_admin"] is False


async def test_create_duplicate_username_409(client: AsyncClient) -> None:
    await setup_admin(client)
    await _create_user(client)
    r = await client.post(
        "/api/users", json={"username": "reader", "password": "otherpass99"}
    )
    assert r.status_code == 409


async def test_reset_password(client: AsyncClient) -> None:
    await setup_admin(client)
    user = await _create_user(client)

    r = await client.patch(f"/api/users/{user['id']}", json={"password": "newpassword99"})
    assert r.status_code == 200

    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json=USER)
    assert r.status_code == 401
    r = await client.post(
        "/api/auth/login", json={"username": "reader", "password": "newpassword99"}
    )
    assert r.status_code == 200


async def test_toggle_admin_and_last_admin_guard(client: AsyncClient) -> None:
    await setup_admin(client)
    user = await _create_user(client)
    admin_id = (await client.get("/api/auth/me")).json()["id"]

    # Promote the reader
    r = await client.patch(f"/api/users/{user['id']}", json={"is_admin": True})
    assert r.status_code == 200
    assert r.json()["is_admin"] is True
    # The new admin can use the admin API
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json=USER)
    assert (await client.get("/api/users")).status_code == 200

    # Demote the original admin (allowed — another admin remains)
    r = await client.patch(f"/api/users/{admin_id}", json={"is_admin": False})
    assert r.status_code == 200
    # Demoting the now-last admin is refused
    r = await client.patch(f"/api/users/{user['id']}", json={"is_admin": False})
    assert r.status_code == 400


async def test_delete_user_guards(client: AsyncClient) -> None:
    await setup_admin(client)
    admin_id = (await client.get("/api/auth/me")).json()["id"]

    # Cannot delete yourself
    assert (await client.delete(f"/api/users/{admin_id}")).status_code == 400
    # Cannot delete the last admin (from a second admin's session)
    second = await _create_user(client, username="admin2", password="admin2pass1", is_admin=True)
    await client.post("/api/auth/logout")
    await client.post(
        "/api/auth/login", json={"username": "admin2", "password": "admin2pass1"}
    )
    assert (await client.delete(f"/api/users/{admin_id}")).status_code == 204
    # admin2 is now the last admin
    assert (await client.delete(f"/api/users/{second['id']}")).status_code == 400
    # Unknown user
    assert (await client.delete("/api/users/9999")).status_code == 404


async def test_delete_user_removes_read_state(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    user = await _create_user(client)

    async with db_session() as session:
        story = Story(title="t", summary="s")
        session.add(story)
        await session.commit()
        session.add(
            StoryState(user_id=user["id"], story_id=story.id, is_read=True, read_at_version=1)
        )
        await session.commit()

    assert (await client.delete(f"/api/users/{user['id']}")).status_code == 204

    async with db_session() as session:
        states = await session.scalar(select(func.count(StoryState.user_id)))
        assert states == 0
        assert await session.get(User, user["id"]) is None

    # Deleted user can no longer log in
    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json=USER)
    assert r.status_code == 401
