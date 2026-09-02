"""SQLite 'database is locked' commit retry (core/db.commit_with_retry)."""

import pytest
from sqlalchemy.exc import OperationalError

from app.core.db import commit_with_retry
from app.models import ActivityEvent


def _locked_error() -> OperationalError:
    return OperationalError("INSERT INTO activity_log ...", {}, Exception("database is locked"))


async def test_commit_with_retry_recovers_from_lock(db_session) -> None:
    """A transient lock must not kill the job: rollback + retry + commit succeeds.

    The rollback discards the pending event, so the prepare callback re-adds it.
    """
    async with db_session() as session:

        async def add_event() -> None:
            session.add(ActivityEvent(component="test", action="retry_ok", detail="{}"))

        await add_event()
        real_commit = session.commit
        attempts = 0

        async def flaky_commit() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _locked_error()
            await real_commit()

        session.commit = flaky_commit  # type: ignore[method-assign]
        await commit_with_retry(session, prepare=add_event, base_delay=0.01)

    assert attempts == 2
    async with db_session() as session:
        rows = await session.execute(
            ActivityEvent.__table__.select().where(ActivityEvent.action == "retry_ok")
        )
        assert len(rows.all()) == 1


async def test_commit_with_retry_gives_up_after_exhaustion(db_session) -> None:
    """Persistent locks propagate (caller job fails, next sweep retries the work)."""
    async with db_session() as session:
        session.add(ActivityEvent(component="test", action="retry_fail", detail="{}"))

        async def always_locked() -> None:
            raise _locked_error()

        session.commit = always_locked  # type: ignore[method-assign]
        with pytest.raises(OperationalError):
            await commit_with_retry(session, attempts=3, base_delay=0.01)


async def test_commit_with_retry_reraises_non_lock_errors(db_session) -> None:
    """Non-lock OperationalErrors are not retried."""
    async with db_session() as session:
        session.add(ActivityEvent(component="test", action="no_retry", detail="{}"))

        async def other_error() -> None:
            raise OperationalError("INSERT ...", {}, Exception("disk I/O error"))

        session.commit = other_error  # type: ignore[method-assign]
        with pytest.raises(OperationalError, match="disk I/O"):
            await commit_with_retry(session, base_delay=0.01)
