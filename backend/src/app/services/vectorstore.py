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

    async def get_story_centroid(self, story_id: int) -> list[float] | None:
        """Return the stored centroid vector, or None."""
        ...

    async def get_story_centroids(self, story_ids: list[int]) -> dict[int, list[float]]:
        """Batch ``get_story_centroid``; stories without a vector are omitted."""
        ...

    async def get_article_vector(self, article_id: int) -> list[float] | None:
        """Return the stored article embedding, or None."""
        ...

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
    """sqlite-vec backed store, sharing the app's SQLite database file.

    Tables are created at app startup (main._ensure_schema_and_seed). We still try
    once per instance as a safety net for direct service use in tests/scripts.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._ensured = False

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

    async def get_story_centroid(self, story_id: int) -> list[float] | None:
        rows = await self.session.execute(
            text("SELECT embedding FROM vec_story WHERE rowid = :id"), {"id": story_id}
        )
        row = rows.first()
        if row is None:
            return None
        return np.frombuffer(row[0], dtype=np.float32).tolist()

    async def get_story_centroids(self, story_ids: list[int]) -> dict[int, list[float]]:
        """Batch read. vec0 point lookups cost ~10 ms each, so read sqlite-vec's chunk
        blobs directly (one blob per 1024 vectors) and slice; fall back to vec0 if the
        shadow-table layout is not what we expect."""
        try:
            return await self._centroids_from_chunks(story_ids)
        except Exception:
            out: dict[int, list[float]] = {}
            for sid in story_ids:
                vec = await self.get_story_centroid(sid)
                if vec:
                    out[sid] = vec
            return out

    async def _centroids_from_chunks(self, story_ids: list[int]) -> dict[int, list[float]]:
        placements: dict[int, list[tuple[int, int]]] = {}
        for i in range(0, len(story_ids), 500):
            ids = story_ids[i : i + 500]
            marks = ",".join(f":i{n}" for n in range(len(ids)))
            rows = await self.session.execute(
                text(
                    "SELECT rowid, chunk_id, chunk_offset FROM vec_story_rowids "
                    f"WHERE rowid IN ({marks})"
                ),
                {f"i{n}": sid for n, sid in enumerate(ids)},
            )
            for rowid, chunk_id, offset in rows:
                placements.setdefault(int(chunk_id), []).append((int(rowid), int(offset)))
        out: dict[int, list[float]] = {}
        width = EMBED_DIM * 4
        for chunk_id, items in placements.items():
            blob = (
                await self.session.execute(
                    text("SELECT vectors FROM vec_story_vector_chunks00 WHERE rowid = :c"),
                    {"c": chunk_id},
                )
            ).scalar_one()
            for rowid, offset in items:
                raw = blob[offset * width : (offset + 1) * width]
                if len(raw) != width:
                    raise ValueError("unexpected vec0 chunk layout")
                out[rowid] = np.frombuffer(raw, dtype=np.float32).tolist()
        return out

    async def get_article_vector(self, article_id: int) -> list[float] | None:
        rows = await self.session.execute(
            text("SELECT embedding FROM vec_article WHERE rowid = :id"), {"id": article_id}
        )
        row = rows.first()
        if row is None:
            return None
        return np.frombuffer(row[0], dtype=np.float32).tolist()

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

    async def get_story_centroid(self, story_id: int) -> list[float] | None:
        vec = self.centroids.get(story_id)
        return None if vec is None else vec.tolist()

    async def get_story_centroids(self, story_ids: list[int]) -> dict[int, list[float]]:
        return {sid: self.centroids[sid].tolist() for sid in story_ids if sid in self.centroids}

    async def get_article_vector(self, article_id: int) -> list[float] | None:
        vec = self.articles.get(article_id)
        return None if vec is None else vec.tolist()

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


async def init_vector_store(session: AsyncSession | None = None) -> VectorStore:
    """Construct + initialize the configured backend at app startup.

    Called once from the lifespan AFTER settings overrides are applied, so a
    GUI-set VECTOR_BACKEND takes effect. Qdrant collections are created here;
    sqlite-vec tables are created by _ensure_schema_and_seed (sync connection).
    """
    global _store
    if settings.vector_backend == "qdrant":
        from app.services.qdrant_store import QdrantVectorStore

        try:
            store = QdrantVectorStore()
            await store.ensure_collections()
        except Exception:
            # Qdrant configured but unreachable — degrade to in-memory so the
            # pipeline keeps running instead of throwing on every article.
            _store = InMemoryVectorStore()
            raise
        _store = store
        return _store
    if settings.vector_backend == "sqlite_vec" and session is not None:
        # sqlite-vec runs on the caller's DB session — do NOT cache it in _store:
        # the startup session closes, and per-op callers must bind a live session.
        sqlite_store = SqliteVecStore(session)
        await sqlite_store.ensure_tables()
        return sqlite_store
    _store = InMemoryVectorStore()
    return _store


def get_vector_store(session: AsyncSession | None = None) -> VectorStore:
    global _store
    if _store is not None:
        return _store
    if settings.vector_backend == "qdrant":
        from app.services.qdrant_store import QdrantVectorStore

        _store = QdrantVectorStore()
        return _store
    if settings.vector_backend == "sqlite_vec" and session is not None:
        return SqliteVecStore(session)
    _store = InMemoryVectorStore()
    return _store


def set_vector_store(store: VectorStore) -> None:
    global _store
    _store = store
