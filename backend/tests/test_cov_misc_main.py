"""main.py: lifespan wiring, legacy stamping, migration subprocess handling, vec tables."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.main as main
from app.core.config import settings
from app.models import Category


def test_find_backend_dir_prefers_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NEWSGATOR_BACKEND_DIR", str(tmp_path))
    assert main._find_backend_dir() == tmp_path


def test_find_backend_dir_walks_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWSGATOR_BACKEND_DIR", raising=False)
    found = main._find_backend_dir()
    assert (found / "alembic.ini").is_file()
    assert (found / "alembic").is_dir()


async def test_legacy_stamp_picks_newest_matching_revision(db_session) -> None:
    from app.core.db import get_engine

    async with get_engine().connect() as conn:
        # create_all schema has every marker → newest stamp
        assert await conn.run_sync(main._legacy_stamp_revision) == main._LEGACY_STAMPS[0][1]


def test_legacy_stamp_falls_back_to_oldest(tmp_path: Path) -> None:
    import sqlalchemy as sa

    eng = sa.create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.connect() as conn:
        conn.execute(sa.text('CREATE TABLE "user" (id INTEGER)'))
        assert main._legacy_stamp_revision(conn) == main._LEGACY_STAMPS[-1][1]
    eng.dispose()


def test_vec_tables_skipped_for_non_sqlite_and_memory(tmp_path: Path) -> None:
    main._create_vec_tables_sync("postgresql://x/y")
    main._create_vec_tables_sync("sqlite+aiosqlite:///:memory:")
    assert not list(tmp_path.iterdir())


def test_vec_tables_created_or_fall_back(tmp_path: Path) -> None:
    path = tmp_path / "v.db"
    main._create_vec_tables_sync(f"sqlite+aiosqlite:///{path}")
    if path.exists():
        conn = sqlite3.connect(path)
        names = {r[0] for r in conn.execute("select name from sqlite_master")}
        conn.close()
        assert {"vec_article", "vec_story"} <= names


def test_vec_tables_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import sqlite_vec

    def boom(_conn: object) -> None:
        raise RuntimeError("no extension")

    monkeypatch.setattr(sqlite_vec, "load", boom)
    with caplog.at_level("WARNING"):
        main._create_vec_tables_sync(f"sqlite+aiosqlite:///{tmp_path}/w.db")
    assert "sqlite-vec unavailable" in caplog.text


class _FakeProc:
    def __init__(self, code: int, out: bytes = b"") -> None:
        self.returncode = code
        self._out = out

    async def communicate(self) -> tuple[bytes, None]:
        return self._out, None


async def test_migrate_schema_stamps_legacy_then_upgrades(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **_kw: object) -> _FakeProc:
        calls.append(args[3:])
        return _FakeProc(0, b"Running upgrade a -> b")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await main._migrate_schema()
    assert calls[0][0] == "stamp"
    assert calls[0][1] == main._LEGACY_STAMPS[0][1]
    assert calls[1] == ("upgrade", "head")


@pytest.mark.parametrize("failing", ["stamp", "upgrade"])
async def test_migrate_schema_failure_raises(
    db_session, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    async def fake_exec(*args: str, **_kw: object) -> _FakeProc:
        return _FakeProc(1 if args[3] == failing else 0, b"bad")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match=f"alembic {failing}"):
        await main._migrate_schema()


async def test_ensure_schema_seeds_categories_once_and_survives_vector_failure(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.vectorstore as vs

    async def boom(_session: object) -> None:
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(vs, "init_vector_store", boom)
    monkeypatch.setattr(main, "_create_vec_tables_sync", lambda _url: None)
    await main._ensure_schema_and_seed()
    async with db_session() as s:
        first = len((await s.scalars(select(Category))).all())
    assert first == len(main.SEED_CATEGORIES)
    await main._ensure_schema_and_seed()
    async with db_session() as s:
        assert len((await s.scalars(select(Category))).all()) == first


async def test_lifespan_non_test_starts_and_stops_workers(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import faulthandler
    import signal

    import app.services.process as process
    import app.services.translation as translation
    import app.workers.scheduler as scheduler

    events: list[str] = []

    async def noop_schema() -> None:
        events.append("schema")

    async def backlog(_session: object) -> int:
        events.append("backlog")
        return 0

    async def stop_async(name: str) -> None:
        events.append(name)

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(main, "init_engine", lambda _url: None)
    monkeypatch.setattr(main, "_ensure_schema_and_seed", noop_schema)
    monkeypatch.setattr(faulthandler, "register", lambda *a, **k: None)
    monkeypatch.setattr(process, "start_worker", lambda: events.append("worker"))
    monkeypatch.setattr(process, "enqueue_backlog", backlog)
    monkeypatch.setattr(process, "stop_worker", lambda: stop_async("stop_worker"))
    monkeypatch.setattr(translation, "start_translation_worker", lambda: events.append("tr"))
    monkeypatch.setattr(translation, "stop_translation_worker", lambda: stop_async("stop_tr"))
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: events.append("sched"))
    monkeypatch.setattr(scheduler, "stop_scheduler", lambda: events.append("stop_sched"))

    try:
        async with main.lifespan(FastAPI()):
            assert events == ["schema", "worker", "tr", "backlog", "sched"]
        assert events[-3:] == ["stop_sched", "stop_worker", "stop_tr"]
    finally:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGUSR2)


async def test_lifespan_in_test_env_only_seeds(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = SimpleNamespace(schema=0)

    async def schema() -> None:
        seen.schema += 1

    monkeypatch.setattr(main, "init_engine", lambda _url: None)
    monkeypatch.setattr(main, "_ensure_schema_and_seed", schema)
    async with main.lifespan(FastAPI()):
        pass
    assert seen.schema == 1
