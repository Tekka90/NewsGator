"""Per-user third-party RSS reader API accounts (SPEC §9).

Scoped to the CURRENT user: every user connects their own reader account.
Passwords and API keys are write-only — accepted on create/patch, never returned.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.schemas import ReaderAccountIn, ReaderAccountOut, ReaderAccountPatch
from app.core.db import get_session
from app.models import ReaderAccount, User
from app.services import activity
from app.services.readers.greader import GReaderAuthError, GReaderClient, GReaderError
from app.services.readers.sync import ensure_virtual_feed, poll_reader_account

router = APIRouter(
    prefix="/reader-accounts", tags=["readers"], dependencies=[Depends(current_user)]
)


async def _own_account(
    session: AsyncSession, account_id: int, user: User
) -> ReaderAccount:
    account = await session.get(ReaderAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reader account not found")
    return account


@router.get("")
async def list_accounts(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> list[ReaderAccountOut]:
    rows = await session.scalars(
        select(ReaderAccount).where(ReaderAccount.user_id == user.id).order_by(ReaderAccount.id)
    )
    return [ReaderAccountOut.model_validate(a) for a in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: ReaderAccountIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ReaderAccountOut:
    account = ReaderAccount(
        user_id=user.id,
        provider=body.provider,
        title=body.title.strip(),
        api_base_url=body.api_base_url.strip(),
        username=body.username.strip(),
        password=body.password.strip(),
        auth_token=body.auth_token.strip() if body.auth_token else None,
    )
    session.add(account)
    await session.flush()

    # Pre-create virtual feed
    feed = await ensure_virtual_feed(session, account)

    await activity.emit(
        session,
        "reader",
        "reader_account_created",
        {
            "account_id": account.id,
            "provider": account.provider,
            "api_base_url": account.api_base_url,
            "virtual_feed_id": feed.id,
        },
    )
    await session.commit()
    await session.refresh(account)
    return ReaderAccountOut.model_validate(account)


@router.patch("/{account_id}")
async def update_account(
    account_id: int,
    body: ReaderAccountPatch,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ReaderAccountOut:
    account = await _own_account(session, account_id, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(account, field, value.strip() if isinstance(value, str) else value)
    await session.commit()
    await session.refresh(account)
    return ReaderAccountOut.model_validate(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    account = await _own_account(session, account_id, user)
    await session.delete(account)
    await activity.emit(
        session,
        "reader",
        "reader_account_deleted",
        {"account_id": account_id, "provider": account.provider},
    )
    await session.commit()


class ReaderTestOut(BaseModel):
    ok: bool
    items_accessible: int = 0
    error: str | None = None


@router.post("/{account_id}/test")
async def test_account(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ReaderTestOut:
    """Probe authentication and stream access (never leaks credentials)."""
    account = await _own_account(session, account_id, user)
    client = GReaderClient(
        api_base_url=account.api_base_url,
        username=account.username,
        password=account.password,
        auth_token=account.auth_token,
    )
    try:
        res = await client.test_connection()
        if client.auth_token != account.auth_token:
            account.auth_token = client.auth_token
            await session.commit()
        return ReaderTestOut(ok=True, items_accessible=res.get("items_accessible", 0))
    except Exception as exc:
        return ReaderTestOut(ok=False, error=str(exc))


class ReaderPollOut(BaseModel):
    new_articles: int
    read_synced: int


@router.post("/{account_id}/poll")
async def poll_account_now(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ReaderPollOut:
    """Poll the reader account immediately."""
    account = await _own_account(session, account_id, user)
    if not account.is_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account is disabled")

    try:
        stats = await poll_reader_account(session, account)
        return ReaderPollOut(**stats)
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Reader poll failed: {exc}") from exc
