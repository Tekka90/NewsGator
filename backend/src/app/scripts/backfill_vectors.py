"""Backfill article/story vectors into the configured vector store.

Run after switching VECTOR_BACKEND (e.g. in-memory/sqlite-vec → qdrant) so the
existing corpus is searchable in the new store. Deterministic: embeddings are
recomputed from persisted article title+summary (SUMMARY_LANGUAGE text, exactly
as the pipeline does), and each story centroid is the mean of its member article
vectors. Idempotent — upserts overwrite.

Usage (from backend/, venv active, env set):
    python -m app.scripts.backfill_vectors            # into the configured store
    python -m app.scripts.backfill_vectors --dry-run  # count only, no writes
"""

import argparse
import asyncio
import time

import numpy as np
from sqlalchemy import select

from app.core.config import settings
from app.core.db import get_session, init_engine
from app.models import Article, Story
from app.services import llm_client, usage
from app.services.vectorstore import init_vector_store

BATCH = 32


async def backfill(dry_run: bool = False) -> None:
    init_engine(settings.database_url)
    async for session in get_session():
        store = await init_vector_store(session)
        print(f"Vector backend: {settings.vector_backend} -> {type(store).__name__}")

        articles = (
            await session.scalars(
                select(Article).where(Article.summary.is_not(None)).order_by(Article.id)
            )
        ).all()
        print(f"Articles with a summary to embed: {len(articles)}")
        if dry_run:
            stories = (await session.scalars(select(Story))).all()
            print(f"Stories (centroids to rebuild): {len(stories)}  [dry-run]")
            return

        # 1) article vectors (batched)
        done = 0
        for i in range(0, len(articles), BATCH):
            chunk = articles[i : i + BATCH]
            texts = [f"{a.title}\n\n{a.summary}" for a in chunk]
            start = time.monotonic()
            vectors = await llm_client.embed(texts)
            batch_latency = int((time.monotonic() - start) * 1000)
            # One usage row per batch: the server's `usage` covers the whole
            # batch, so recording per-article would multiply the tokens.
            usage.record(
                session,
                "backfill_embed",
                endpoint="embed",
                model=settings.embed_model,
                latency_ms=batch_latency,
                prompt_chars=sum(len(t) for t in texts),
            )
            for article, vec in zip(chunk, vectors, strict=True):
                await store.upsert_article(article.id, vec)
            done += len(chunk)
            print(f"  articles embedded {done}/{len(articles)}")
        await session.commit()

        # 2) story centroids = mean of member article vectors
        stories = (await session.scalars(select(Story))).all()
        rebuilt = 0
        for story in stories:
            member = (
                await session.scalars(
                    select(Article).where(
                        Article.story_id == story.id, Article.summary.is_not(None)
                    )
                )
            ).all()
            if not member:
                continue
            vecs = await llm_client.embed([f"{a.title}\n\n{a.summary}" for a in member])
            centroid = np.mean(np.asarray(vecs, dtype=np.float32), axis=0)
            await store.upsert_story_centroid(story.id, centroid.tolist())
            rebuilt += 1
        print(f"Story centroids rebuilt: {rebuilt}/{len(stories)}")
        print("Backfill complete.")
        break


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill vectors into the vector store")
    parser.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
