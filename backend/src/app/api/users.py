"""Admin user-management routes.

The first-run setup (POST /auth/setup) only ever creates one account; this
router is how additional users come to exist. Guards: the last admin can
neither be demoted nor deleted, and a user cannot delete themselves.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_user
from app.api.schemas import ManagedUserOut, UserCreateIn, UserPatchIn
from app.core.db import get_session
from app.core.security import hash_password
from app.models import StoryState, User
from app.services import activity

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(admin_user)])


@router.get("")
async def list_users(session: AsyncSession = Depends(get_session)) -> list[ManagedUserOut]:
    users = (await session.scalars(select(User).order_by(User.id))).all()
    return [ManagedUserOut.model_validate(u) for u in users]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateIn, session: AsyncSession = Depends(get_session)
) -> ManagedUserOut:
    taken = await session.scalar(select(User.id).where(User.username == body.username))
    if taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        is_admin=body.is_admin,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await activity.emit(
        session, "users", "user_created",
        {"user_id": user.id, "username": user.username, "is_admin": user.is_admin},
    )
    await session.commit()
    return ManagedUserOut.model_validate(user)


@router.patch("/{user_id}")
async def update_user(
    user_id: int, body: UserPatchIn, session: AsyncSession = Depends(get_session)
) -> ManagedUserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if body.is_admin is False and user.is_admin:
        await _ensure_not_last_admin(session, user)
        user.is_admin = False
    elif body.is_admin is True:
        user.is_admin = True
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    await session.commit()
    await session.refresh(user)
    return ManagedUserOut.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    admin: User = Depends(admin_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete yourself")
    if user.is_admin:
        await _ensure_not_last_admin(session, user)
    # Per-user read state has no delete cascade — bulk-delete by hand.
    await session.execute(delete(StoryState).where(StoryState.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await activity.emit(
        session, "users", "user_deleted",
        {"user_id": user_id, "username": user.username},
    )
    await session.commit()


async def _ensure_not_last_admin(session: AsyncSession, user: User) -> None:
    """Raise 400 if demoting/deleting `user` would leave zero admins."""
    others = await session.scalar(
        select(func.count(User.id)).where(User.is_admin, User.id != user.id)
    )
    if not others:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Cannot remove the last admin"
        )
