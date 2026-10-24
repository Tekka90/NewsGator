"""Coverage tests: vector store backends and factory."""

import pytest

from app.core.config import settings
from app.services import vectorstore
from app.services.vectorstore import (
    EMBED_DIM,
    InMemoryVectorStore,
    SqliteVecStore,
    cosine_similarity,
    get_vector_store,
    init_vector_store,
    set_vector_store,
)


@pytest.fixture(autouse=True)
def _reset_store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vectorstore, "_store", None)
    yield
    monkeypatch.setattr(vectorstore, "_store", None)


def _vec(*head: float) -> list[float]:
    return [*head] + [0.0] * (EMBED_DIM - len(head))


def test_cosine_similarity_zero_vector_is_zero() -> None:
    import numpy as np

    assert cosine_similarity(np.zeros(3), np.ones(3)) == 0.0
    assert cosine_similarity(np.array([1.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(1.0)


async def test_sqlite_vec_store_roundtrip(db_session) -> None:
    async with db_session() as s:
        store = SqliteVecStore(s)
        await store.ensure_tables()
        await store.upsert_article(1, _vec(1.0))
        await store.upsert_article(1, _vec(0.0, 1.0))  # upsert replaces
        assert (await store.get_article_vector(1))[:2] == [0.0, 1.0]  # type: ignore[index]
        assert await store.get_article_vector(99) is None

        await store.upsert_story_centroid(10, _vec(1.0))
        await store.upsert_story_centroid(11, _vec(0.0, 5.0))
        assert (await store.get_story_centroid(10))[0] == 1.0  # type: ignore[index]
        assert await store.get_story_centroid(404) is None

        hits = await store.search_story_centroids(_vec(1.0), limit=2)
        assert [h[0] for h in hits] == [10, 11]
        assert hits[0][1] == pytest.approx(1.0)  # distance 0 -> similarity 1
        assert 0 < hits[1][1] < hits[0][1]
        assert len(await store.search_story_centroids(_vec(1.0), limit=1)) == 1

        await store.delete_article(1)
        await store.delete_story(10)
        assert await store.get_article_vector(1) is None
        assert await store.get_story_centroid(10) is None
        assert await store.get_story_centroid(11) is not None


async def test_in_memory_store_ranks_by_cosine_and_deletes() -> None:
    store = InMemoryVectorStore()
    await store.upsert_story_centroid(1, [1.0, 0.0])
    await store.upsert_story_centroid(2, [1.0, 1.0])
    await store.upsert_story_centroid(3, [0.0, 1.0])
    hits = await store.search_story_centroids([1.0, 0.1], limit=2)
    assert [h[0] for h in hits] == [1, 2]
    await store.upsert_article(5, [0.25, 0.5])
    assert await store.get_article_vector(5) == [0.25, 0.5]
    assert await store.get_story_centroid(404) is None
    await store.delete_article(5)
    await store.delete_story(1)
    await store.delete_story(1)  # idempotent
    assert await store.get_article_vector(5) is None
    assert await store.get_story_centroid(1) is None


async def test_get_vector_store_selection(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    async with db_session() as s:
        store = get_vector_store(s)
        assert isinstance(store, SqliteVecStore)
        assert vectorstore._store is None  # per-session store is never cached

    # without a session: in-memory fallback, cached
    mem = get_vector_store()
    assert isinstance(mem, InMemoryVectorStore)
    assert get_vector_store() is mem

    custom = InMemoryVectorStore()
    set_vector_store(custom)
    assert get_vector_store() is custom


async def test_get_vector_store_qdrant_backend(monkeypatch) -> None:
    from app.services import qdrant_store

    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "http://q.test")
    monkeypatch.setattr(qdrant_store, "AsyncQdrantClient", lambda **kw: object())
    store = get_vector_store()
    assert isinstance(store, qdrant_store.QdrantVectorStore)
    assert get_vector_store() is store


async def test_init_vector_store_sqlite_vec_creates_tables(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    async with db_session() as s:
        store = await init_vector_store(s)
        assert isinstance(store, SqliteVecStore)
        await store.upsert_article(3, _vec(2.0))
        assert await store.get_article_vector(3) is not None
        assert vectorstore._store is None


async def test_init_vector_store_defaults_to_memory(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vector_backend", "sqlite_vec")
    store = await init_vector_store(None)
    assert isinstance(store, InMemoryVectorStore)
    assert vectorstore._store is store


async def test_init_vector_store_qdrant_failure_degrades_to_memory(monkeypatch) -> None:
    from app.services import qdrant_store

    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "http://q.test")
    monkeypatch.setattr(qdrant_store, "AsyncQdrantClient", lambda **kw: object())

    async def boom(self) -> None:
        raise RuntimeError("unreachable")

    monkeypatch.setattr(qdrant_store.QdrantVectorStore, "ensure_collections", boom)
    with pytest.raises(RuntimeError, match="unreachable"):
        await init_vector_store()
    assert isinstance(vectorstore._store, InMemoryVectorStore)


async def test_init_vector_store_qdrant_success(monkeypatch) -> None:
    from app.services import qdrant_store

    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "http://q.test")
    monkeypatch.setattr(qdrant_store, "AsyncQdrantClient", lambda **kw: object())

    async def ok(self) -> None:
        return None

    monkeypatch.setattr(qdrant_store.QdrantVectorStore, "ensure_collections", ok)
    store = await init_vector_store()
    assert isinstance(store, qdrant_store.QdrantVectorStore)
    assert vectorstore._store is store
