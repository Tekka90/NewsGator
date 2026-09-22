"""Per-user IMAP accounts for newsletter ingestion (SPEC §9, mail path).

Unlike most routers this one is scoped to the CURRENT user (not admin): every
user connects their own mailbox folder. Passwords are write-only — accepted on
create/patch, never returned.
"""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.schemas import MailAccountIn, MailAccountOut, MailAccountPatch
from app.core.config import settings
from app.core.db import get_session
from app.models import MailAccount, User
from app.services import activity, mailnews

router = APIRouter(
    prefix="/mail-accounts", tags=["mail"], dependencies=[Depends(current_user)]
)


async def _own_account(
    session: AsyncSession, account_id: int, user: User
) -> MailAccount:
    account = await session.get(MailAccount, account_id)
    # 404 for other users' accounts too — no existence leak
    if account is None or account.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mail account not found")
    return account


@router.get("")
async def list_accounts(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> list[MailAccountOut]:
    rows = await session.scalars(
        select(MailAccount).where(MailAccount.user_id == user.id).order_by(MailAccount.id)
    )
    return [MailAccountOut.model_validate(a) for a in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: MailAccountIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> MailAccountOut:
    account = MailAccount(
        user_id=user.id,
        host=body.host.strip(),
        port=body.port,
        username=body.username.strip(),
        password=body.password,
        folder=body.folder.strip(),
        use_ssl=body.use_ssl,
    )
    session.add(account)
    await activity.emit(
        session,
        "mail",
        "mail_account_created",
        {"account": f"{account.username}@{account.host}", "folder": account.folder},
    )
    await session.commit()
    await session.refresh(account)
    return MailAccountOut.model_validate(account)


@router.patch("/{account_id}")
async def update_account(
    account_id: int,
    body: MailAccountPatch,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> MailAccountOut:
    account = await _own_account(session, account_id, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(account, field, value.strip() if isinstance(value, str) else value)
    await session.commit()
    await session.refresh(account)
    return MailAccountOut.model_validate(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    account = await _own_account(session, account_id, user)
    # Mail feeds created from this account are kept: their articles/stories are
    # shared across users and remain useful history.
    await session.delete(account)
    await activity.emit(
        session,
        "mail",
        "mail_account_deleted",
        {"account": f"{account.username}@{account.host}"},
    )
    await session.commit()


class MailTestOut(BaseModel):
    ok: bool
    errors: list[str]
    folder: str | None = None


@router.post("/{account_id}/test")
async def test_account(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> MailTestOut:
    """Probe IMAP login + folder existence (never leaks the password)."""
    account = await _own_account(session, account_id, user)
    result = await mailnews.test_account(account)
    return MailTestOut(**result)


class MailPollOut(BaseModel):
    """Immediate answer: how many messages were found. Processing runs in the
    background and is followed live on the Activity page (SSE events)."""

    found: int
    processing: bool = True


# Strong refs to in-flight background mail processing (loop only weak-refs tasks)
_background_mail: set[asyncio.Task[None]] = set()


async def _process_in_background(account_id: int, uids: list[int]) -> None:
    """Fetch bodies + process messages with a fresh session; failures stay on
    events (mail_fetch_error / mail_process_error) and account.last_error.
    Releases the per-account poll lock on the way out (started in the poll
    endpoint)."""
    try:
        async with asyncio.timeout(mailnews.POLL_LOCK_TIMEOUT_S):
            async for session in get_session():
                account = await session.get(MailAccount, account_id)
                if account is None:
                    return
                try:
                    messages = await mailnews.fetch_messages_by_uid(account, uids)
                except Exception as exc:
                    account.last_checked_at = datetime.now(UTC)
                    account.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                    await activity.emit(
                        session,
                        "mail",
                        "mail_fetch_error",
                        {
                            "account": f"{account.username}@{account.host}",
                            "uids": len(uids),
                            "error": account.last_error,
                        },
                        level="error",
                    )
                    await session.commit()
                    return
                try:
                    await mailnews.process_messages(session, account, messages)
                except Exception as exc:
                    await session.rollback()
                    await activity.emit(
                        session,
                        "mail",
                        "mail_process_error",
                        {"account_id": account_id, "error": f"{type(exc).__name__}: {exc}"[:500]},
                        level="error",
                    )
                    await session.commit()
                break
    except TimeoutError:
        async for session in get_session():
            account = await session.get(MailAccount, account_id)
            if account is not None:
                account.last_checked_at = datetime.now(UTC)
                mins = int(mailnews.POLL_LOCK_TIMEOUT_S // 60)
                account.last_error = f"Poll background task timed out after {mins} minutes"
                await activity.emit(
                    session,
                    "mail",
                    "mail_process_error",
                    {"account_id": account_id, "error": account.last_error},
                    level="error",
                )
                await session.commit()
            break
    finally:
        mailnews.end_poll(account_id)


@router.post("/{account_id}/poll", status_code=status.HTTP_202_ACCEPTED)
async def poll_account_now(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> MailPollOut:
    """Poll now: SEARCH for new messages and answer immediately with the count.

    Only the fast search phase is synchronous (the button greys until this
    returns); the body downloads + full processing run in the background —
    watch progress on the Activity page.
    """
    account = await _own_account(session, account_id, user)
    if not account.is_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Account is disabled")
    # One poll at a time per account: the button re-enables after the search
    # phase while processing runs in the background for minutes — a second
    # click would redo the same messages (watermark not yet advanced).
    if not mailnews.try_begin_poll(account.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A poll is already running for this account — follow it on the Activity page",
        )
    await activity.emit(
        session,
        "mail",
        "mail_poll_start",
        {"account": f"{account.username}@{account.host}", "folder": account.folder},
    )
    await session.commit()
    try:
        uids = await mailnews.search_new_uids(account)
    except Exception as exc:
        mailnews.end_poll(account.id)
        account.last_checked_at = datetime.now(UTC)
        account.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        await activity.emit(
            session,
            "mail",
            "mail_poll_failed",
            {"account": f"{account.username}@{account.host}", "error": account.last_error},
            level="error",
        )
        await session.commit()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"IMAP poll failed: {account.last_error}"
        ) from None

    if not uids:
        mailnews.end_poll(account.id)
        await activity.emit(
            session,
            "mail",
            "mail_poll_done",
            {
                "account": f"{account.username}@{account.host}",
                "folder": account.folder,
                "messages": 0,
                "new_articles": 0,
                "skipped_old": 0,
            },
        )
        account.last_checked_at = datetime.now(UTC)
        account.last_error = None
        await session.commit()
        return MailPollOut(found=0, processing=False)

    if settings.environment != "test":
        task = asyncio.create_task(_process_in_background(account.id, uids))
        _background_mail.add(task)
        task.add_done_callback(_background_mail.discard)
    else:  # tests: process inline so assertions see the result
        await _process_in_background(account.id, uids)
    return MailPollOut(found=len(uids), processing=settings.environment != "test")
