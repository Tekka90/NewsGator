"""Qdrant-backed vector store (SPEC §2: external Qdrant, optional).

Configured via VECTOR_BACKEND=qdrant + QDRANT_URL (+ QDRANT_API_KEY). The project
never spins Qdrant up — it must be reachable on the network.
"""

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.core.config import settings
from app.services.vectorstore import EMBED_DIM

ARTICLES = "articles"
STORIES = "stories"


class QdrantVectorStore:
    def __init__(self) -> None:
        if not settings.qdrant_url:
            raise RuntimeError("QDRANT_URL must be set when VECTOR_BACKEND=qdrant")
        self.client = AsyncQdrantClient(
            url=settings.qdrant_url, api_key=settings.qdrant_api_key
        )

    async def ensure_collections(self) -> None:
        for name in (ARTICLES, STORIES):
            if not await self.client.collection_exists(name):
                await self.client.create_collection(
                    name, vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE)
                )

    async def upsert_article(self, article_id: int, vector: list[float]) -> None:
        await self.client.upsert(
            ARTICLES, points=[PointStruct(id=article_id, vector=vector)]
        )

    async def upsert_story_centroid(self, story_id: int, vector: list[float]) -> None:
        await self.client.upsert(
            STORIES, points=[PointStruct(id=story_id, vector=vector)]
        )

    async def get_story_centroid(self, story_id: int) -> list[float] | None:
        points = await self.client.retrieve(STORIES, ids=[story_id], with_vectors=True)
        if not points or points[0].vector is None:
            return None
        vec = points[0].vector
        # qdrant vector type is a union; accept only a plain float list
        if isinstance(vec, list) and (not vec or isinstance(vec[0], (int, float))):
            return [float(x) for x in vec]  # type: ignore[arg-type]
        return None

    async def search_story_centroids(
        self, vector: list[float], *, limit: int = 5
    ) -> list[tuple[int, float]]:
        if not await self.client.collection_exists(STORIES):
            return []
        result = await self.client.query_points(STORIES, query=vector, limit=limit)
        return [(int(p.id), float(p.score)) for p in result.points]

    async def delete_article(self, article_id: int) -> None:
        await self.client.delete(ARTICLES, points_selector=[article_id])

    async def delete_story(self, story_id: int) -> None:
        await self.client.delete(STORIES, points_selector=[story_id])
