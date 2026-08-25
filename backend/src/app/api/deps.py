"""Auth dependencies for FastAPI routers."""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import SESSION_COOKIE, parse_session_token
from app.models import User


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    # Cookie first (browsers), then Bearer token, then ?token= (SSE cannot set
    # headers, and iOS standalone PWAs do not reliably persist cookies).
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        token = request.query_params.get("token")
    user_id = parse_session_token(token) if token else None
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user


async def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user
