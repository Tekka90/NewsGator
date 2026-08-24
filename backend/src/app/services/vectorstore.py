"""Vector store abstraction (SPEC §2).

Pipeline code talks to the `VectorStore` protocol only — sqlite-vec (default,
zero-ops) or external Qdrant behind the same interface. Never import
backend-specific vector code in pipeline modules.
"""

from typing import Protocol

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

# Embedding dimension is tied to EMBED_MODEL (bge-m3 / e5-large → 1024).
EMBED_DIM = 1024


class VectorStore(Protocol):
    async def upsert_article(self, article_id: int, vector: list[float]) -> None: ...

    async def upsert_story_centroid(self, story_id: int, vector: list[float]) -> None: ...

    async def search_story_centroids(
        self, vector: list[float], *, limit: int = 5
    ) -> list[tuple[int, float]]:
        """Return [(story_id, cosine_similarity)] best-first."""
        ...

    async def delete_article(self, article_id: int) -> None: ...

    async def delete_story(self, story_id: int) -> None: ...


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


class SqliteVecStore:
    """sqlite-vec backed store, sharing the app's SQLite database file."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _exec(self, sql: str, params: dict[str, object] | None = None) -> None:
        await self.session.execute(text(sql), params or {})

    async def ensure_tables(self) -> None:
        await self._exec(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_article "
            f"USING vec0(embedding float[{EMBED_DIM}])"
        )
        await self._exec(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_story "
            f"USING vec0(embedding float[{EMBED_DIM}])"
        )

    async def upsert_article(self, article_id: int, vector: list[float]) -> None:
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        await self._exec("DELETE FROM vec_article WHERE rowid = :id", {"id": article_id})
        await self._exec(
            "INSERT INTO vec_article (rowid, embedding) VALUES (:id, :v)",
            {"id": article_id, "v": blob},
        )

    async def upsert_story_centroid(self, story_id: int, vector: list[float]) -> None:
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        await self._exec("DELETE FROM vec_story WHERE rowid = :id", {"id": story_id})
        await self._exec(
            "INSERT INTO vec_story (rowid, embedding) VALUES (:id, :v)",
            {"id": story_id, "v": blob},
        )

    async def search_story_centroids(
        self, vector: list[float], *, limit: int = 5
    ) -> list[tuple[int, float]]:
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        rows = await self.session.execute(
            text(
                "SELECT rowid, distance FROM vec_story "
                "WHERE embedding MATCH :v AND k = :k"
            ),
            {"v": blob, "k": limit},
        )
        # vec0 with float vectors uses L2 distance by default; convert to a
        # similarity in (0, 1]. Good enough for candidate ranking at v1 scale;
        # exact cosine re-rank happens on candidates if needed.
        return [(int(r[0]), 1.0 / (1.0 + float(r[1]))) for r in rows]

    async def delete_article(self, article_id: int) -> None:
        await self._exec("DELETE FROM vec_article WHERE rowid = :id", {"id": article_id})

    async def delete_story(self, story_id: int) -> None:
        await self._exec("DELETE FROM vec_story WHERE rowid = :id", {"id": story_id})


class InMemoryVectorStore:
    """Fallback store used when sqlite-vec is unavailable (and in tests).

    At v1 scale (~thousands of vectors), exact cosine over a dict is fine.
    """

    def __init__(self) -> None:
        self.articles: dict[int, np.ndarray] = {}
        self.centroids: dict[int, np.ndarray] = {}

    async def upsert_article(self, article_id: int, vector: list[float]) -> None:
        self.articles[article_id] = np.asarray(vector, dtype=np.float32)

    async def upsert_story_centroid(self, story_id: int, vector: list[float]) -> None:
        self.centroids[story_id] = np.asarray(vector, dtype=np.float32)

    async def search_story_centroids(
        self, vector: list[float], *, limit: int = 5
    ) -> list[tuple[int, float]]:
        v = np.asarray(vector, dtype=np.float32)
        scored = sorted(
            ((sid, cosine_similarity(v, c)) for sid, c in self.centroids.items()),
            key=lambda t: t[1],
            reverse=True,
        )
        return scored[:limit]

    async def delete_article(self, article_id: int) -> None:
        self.articles.pop(article_id, None)

    async def delete_story(self, story_id: int) -> None:
        self.centroids.pop(story_id, None)


# Process-wide store instance (set during app startup).
_store: VectorStore | None = None


def get_vector_store(session: AsyncSession | None = None) -> VectorStore:
    global _store
    if _store is not None:
        return _store
    if settings.vector_backend == "sqlite_vec" and session is not None:
        return SqliteVecStore(session)
    _store = InMemoryVectorStore()
    return _store


def set_vector_store(store: VectorStore) -> None:
    global _store
    _store = store
