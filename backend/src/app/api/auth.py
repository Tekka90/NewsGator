"""Auth routes: first-run setup, login, logout, me."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.schemas import AuthOut, LoginIn, MePatch, SetupIn, UserOut
from app.core.db import get_session
from app.core.security import (
    SESSION_COOKIE,
    hash_password,
    make_session_token,
    parse_session_token,
    verify_password,
)
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/setup-needed")
async def setup_needed(session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    count = await session.scalar(select(func.count(User.id)))
    return {"setup_needed": count == 0}


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup(
    body: SetupIn, response: Response, session: AsyncSession = Depends(get_session)
) -> AuthOut:
    count = await session.scalar(select(func.count(User.id)))
    if count:
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup already completed")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        is_admin=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _auth_response(response, user)


@router.post("/login")
async def login(
    body: LoginIn, response: Response, session: AsyncSession = Depends(get_session)
) -> AuthOut:
    user = await session.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    return _auth_response(response, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


@router.get("/me")
async def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/session-debug")
async def session_debug(request: Request) -> dict[str, object]:
    """Which credential was presented and whether it validated.

    Diagnostic for PWA session persistence: answers "did the token survive the
    app restart?" (client side) vs "is the token being rejected?" (server side,
    e.g. SECRET_KEY changed). Cookie value is only reported as present/absent.
    """
    auth = request.headers.get("authorization", "")
    presented = "none"
    token = None
    if auth.lower().startswith("bearer "):
        presented, token = "bearer", auth[7:].strip()
    elif request.cookies.get(SESSION_COOKIE):
        presented, token = "cookie", request.cookies.get(SESSION_COOKIE)
    elif request.query_params.get("token"):
        presented, token = "query", request.query_params.get("token")
    return {
        "presented": presented,
        "token_valid": parse_session_token(token) is not None if token else False,
    }


@router.patch("/me")
async def patch_me(
    body: MePatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    if body.summary_language is not None:
        user.summary_language = body.summary_language
    if body.story_sort is not None:
        user.story_sort = body.story_sort
    if body.story_order is not None:
        user.story_order = body.story_order
    if body.story_filter is not None:
        user.story_filter = body.story_filter
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    await session.commit()
    return UserOut.model_validate(user)


def _auth_response(response: Response, user: User) -> AuthOut:
    """Set the cookie AND return the token in the body (PWA-safe auth)."""
    token = make_session_token(user.id)
    _set_cookie(response, user.id)
    return AuthOut(**UserOut.model_validate(user).model_dump(), token=token)


def _set_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(user_id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
