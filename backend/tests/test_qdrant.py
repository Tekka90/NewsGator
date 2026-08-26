"""Qdrant vector-store init: collections are created at startup."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import vectorstore
from app.services.vectorstore import InMemoryVectorStore, init_vector_store


@pytest.fixture(autouse=True)
def reset_store(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vectorstore, "_store", None)
    yield
    monkeypatch.setattr(vectorstore, "_store", None)


async def test_qdrant_backend_creates_collections(
    monkeypatch: pytest.MonkeyPatch, db_session: async_sessionmaker[AsyncSession]
) -> None:
    """VECTOR_BACKEND=qdrant → collections ensured at startup (regression: they
    were never created, so every Qdrant op failed)."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant.test:6333")

    created: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs): ...

        async def collection_exists(self, name: str) -> bool:
            return False

        async def create_collection(self, name, vectors_config):
            created.append(name)

    from app.services import qdrant_store

    monkeypatch.setattr(qdrant_store, "AsyncQdrantClient", FakeClient)

    # ensure_collections now probes the embed endpoint for the real dimension
    from app.services import llm_client

    async def fake_embed(texts, model=None):
        return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(llm_client, "embed", fake_embed)

    async with db_session() as s:
        store = await init_vector_store(s)
    assert set(created) == {qdrant_store.ARTICLES, qdrant_store.STORIES}
    assert vectorstore._store is store


async def test_qdrant_unreachable_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch, db_session: async_sessionmaker[AsyncSession]
) -> None:
    """Configured but unreachable Qdrant → in-memory fallback (pipeline keeps
    running), and the init error propagates so startup logs it."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "vector_backend", "qdrant")
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant.test:6333")

    class BoomClient:
        def __init__(self, **kwargs): ...

        async def collection_exists(self, name: str) -> bool:
            raise ConnectionError("refused")

    from app.services import qdrant_store

    monkeypatch.setattr(qdrant_store, "AsyncQdrantClient", BoomClient)

    # embed probe must succeed so we reach the Qdrant (failing) call
    from app.services import llm_client

    async def fake_embed(texts, model=None):
        return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(llm_client, "embed", fake_embed)

    async with db_session() as s:
        with pytest.raises(ConnectionError):
            await init_vector_store(s)
    assert isinstance(vectorstore._store, InMemoryVectorStore)
