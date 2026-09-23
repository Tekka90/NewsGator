"""Opt-in isolated *real* NewsGator HTTP server for Apple integration tests.

From backend: .venv/bin/python tests/apple_smoke_server.py --port 18765
Stop with SIGTERM/Ctrl-C; its bounded TemporaryDirectory is then removed.
No environment variables are required: database/test/vector/provider settings are
forced before importing the application. The process never uses the user's DB.
Only /fixture/* supplies local source content; all /api/* routes are production.
Chat/pipeline inference, IMAP and Readeck are deterministic doubles, not model
validation. The real article worker runs, but the scheduler stays disabled.
"""

import argparse
import asyncio
import ipaddress
import os
import re
import signal
import socket
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode

ADMIN = {"username": "apple-admin", "password": "apple-admin-pass"}
READER = {"username": "apple-reader", "password": "apple-reader-pass"}
STAMP = datetime(2026, 9, 1, 12, tzinfo=UTC)
TESTS = Path(__file__).resolve().parent


def local_network_only(event: str, args: tuple) -> None:
    """Reject DNS/network destinations outside loopback, including accidental probes."""
    host = None
    if event == "socket.getaddrinfo":
        host = args[0]
    elif event == "socket.connect" and isinstance(args[1], tuple):
        host = args[1][0]
    if host is None or host == "localhost":
        return
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise OSError("Apple fixture permits loopback network destinations only")


def make_app(url: str):
    import httpx
    from fastapi.responses import Response
    from sqlalchemy import select

    from app.core import db
    from app.core.config import settings
    from app.main import create_app, lifespan
    from app.models import Article, Feed, Story, StoryRevision, StoryState, User, UserFeed
    from app.services import chat, llm_client, mailnews, process, readeck, vectorstore

    async def embed_question(question):
        return [[1.0, 0.0, 0.0]]

    async def answer_question(system, user):
        ids = [int(value) for value in re.findall(r"\[Story (\d+)\]", user)]
        story_id = 1 if 1 in ids else ids[0]
        return {
            "answer": f"Deterministic fixture answer [Story {story_id}].",
            "story_ids": [story_id],
        }, 7

    async def pipeline_embedding(texts):
        return [[0.0, 0.0, 1.0] for _ in texts]

    async def pipeline_completion(system, user, **kwargs):
        return {
            "summary": "Facts fetched through actual local HTTP RSS ingestion.",
            "category": "Tech", "headline": "Locally ingested RSS story",
            "new_facts": False, "same_event": False,
        }, 5

    async def test_mail(account):
        return {"ok": True, "errors": [], "folder": account.folder}

    async def mail_uids(account):
        return []

    async def save_bookmark(story, articles):
        return {
            "bookmark_id": f"fixture-bookmark-{story.id}",
            "href": f"{url}/fixture/bookmark/{story.id}",
            "latency_ms": 3,
        }

    @asynccontextmanager
    async def fixture_lifespan(app):
        with (
            patch.object(chat, "_embed_query", embed_question),
            patch.object(chat, "_answer", answer_question),
            patch.object(llm_client, "embed", pipeline_embedding),
            patch.object(llm_client, "chat_json", pipeline_completion),
            patch.object(mailnews, "test_account", test_mail),
            patch.object(mailnews, "search_new_uids", mail_uids),
            patch.object(readeck, "save_story", save_bookmark),
        ):
            try:
                async with lifespan(app):
                    store = vectorstore.get_vector_store()
                    # Auth accounts/tokens are created by the actual auth/user routes.
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=url,
                        trust_env=False,
                    ) as client:
                        response = await client.post("/api/auth/setup", json=ADMIN)
                        response.raise_for_status()
                        response = await client.post("/api/users", json=READER)
                        response.raise_for_status()
                        response = await client.post("/api/feeds", json={
                            "url": f"{url}/fixture/rss", "title": "Apple RSS",
                            "fetch_fulltext": False, "backfill_days": 0,
                        })
                        response.raise_for_status()
                    async for session in db.get_session():
                        reader = await session.scalar(
                            select(User).where(User.username == READER["username"])
                        )
                        rss = await session.get(Feed, 1)
                        mail = Feed(
                            url="newsletter:newsletter@example.invalid",
                            title="Apple Mail", kind="mail",
                            sender_email="newsletter@example.invalid",
                            fetch_fulltext=False, email_count=3,
                        )
                        session.add(mail)
                        await session.flush()
                        for index, feed in enumerate([rss, mail], start=1):
                            story = Story(
                                id=index,
                                title=f"Apple contract {'RSS' if index == 1 else 'mail'} story",
                                summary="Updated RSS facts." if index == 1 else "Newsletter intro.",
                                category="Tech" if index == 1 else "Science",
                                version=2 if index == 1 else 1,
                                first_seen_at=STAMP, last_updated_at=STAMP,
                            )
                            session.add(story)
                            await session.flush()
                            session.add(Article(
                                id=index, feed_id=feed.id, guid=f"seed-{index}",
                                url=f"{url}/fixture/source/{index}",
                                title="RSS source" if index == 1 else "Newsletter source",
                                raw_content=story.summary, full_text=story.summary,
                                summary=story.summary, category=story.category, language="en",
                                newsletter_intro=story.summary if index == 2 else None,
                                story_id=story.id, processing_state="clustered",
                                published_at=STAMP if index == 1 else None, fetched_at=STAMP,
                            ))
                            await store.upsert_story_centroid(
                                story.id, [1.0, 0.0, 0.0] if index == 1 else [0.0, 1.0, 0.0]
                            )
                            await store.upsert_article(
                                index, [1.0, 0.0, 0.0] if index == 1 else [0.0, 1.0, 0.0]
                            )
                        session.add_all([
                            UserFeed(user_id=reader.id, feed_id=rss.id),
                            UserFeed(user_id=reader.id, feed_id=mail.id),
                            UserFeed(user_id=1, feed_id=mail.id),
                            StoryRevision(
                                story_id=1, version=1, summary="Original RSS facts.",
                                created_at=STAMP,
                            ),
                            StoryRevision(
                                story_id=1, version=2, summary="Updated RSS facts.",
                                created_at=STAMP,
                            ),
                            StoryState(
                                user_id=reader.id, story_id=1, is_read=True,
                                read_at_version=1, read_at=STAMP,
                            ),
                        ])
                        await session.commit()
                        break
                    print(url, flush=True)
                    print(f"{READER['username']} / {READER['password']}", flush=True)
                    print(f"{ADMIN['username']} / {ADMIN['password']}", flush=True)
                    process.start_worker()
                    yield
            finally:
                await process.stop_worker()
                await db.get_engine().dispose()

    app = create_app()
    app.router.lifespan_context = fixture_lifespan

    @app.get("/fixture/identity", include_in_schema=False)
    async def fixture_identity():
        return {"fixture": "newsgator-apple-smoke", "isolated": True}

    @app.get("/fixture/rss", include_in_schema=False)
    async def local_rss(client: str = ""):
        suffix = "?" + urlencode({"client": client}) if client else ""
        source = escape(url + "/fixture/source/3" + suffix)
        guid = escape("local-ingest-" + (client or "1"))
        return Response(
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            '<title>Apple RSS</title><link>' + url
            + '</link><description>Local fixture</description>'
            '<item><guid>' + guid + '</guid><title>Locally ingested RSS item</title>'
            '<link>' + source + '</link>'
            '<description>Facts fetched through actual local HTTP RSS ingestion.</description>'
            '</item></channel></rss>',
            media_type="application/rss+xml",
        )

    @app.get("/fixture/source/{source_id}", include_in_schema=False)
    async def local_source(source_id: int):
        return Response(
            f"<html><head><title>Fixture {source_id}</title></head>"
            "<body><article><p>Local deterministic news source.</p></article></body></html>",
            media_type="text/html",
        )

    assert settings.environment == "test"
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=18765, help="Loopback port; 0 chooses a free port"
    )
    args = parser.parse_args()
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error("port must be 0 or between 1024 and 65535")
    sys.addaudithook(local_network_only)
    # Uvicorn re-raises SIGTERM after graceful shutdown; unwind the temp-directory context.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with TemporaryDirectory(prefix=".apple-smoke-", dir=TESTS) as directory:
        print(f"fixture-directory:{directory}", flush=True)
        os.environ.update({
            "DATABASE_URL": f"sqlite+aiosqlite:///{Path(directory) / 'fixture.db'}",
            "ENVIRONMENT": "test", "VECTOR_BACKEND": "memory",
            "SECRET_KEY": os.urandom(32).hex(),
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "EMBED_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_API_KEY": "fixture-only", "LLM_MODEL": "fixture-chat",
            "EMBED_MODEL": "fixture-embed", "QDRANT_URL": "", "QDRANT_API_KEY": "",
            "READECK_BASE_URL": "http://127.0.0.1:9", "READECK_TOKEN": "fixture-only",
            "CHAT_ENABLED": "true", "SUMMARY_LANGUAGE": "en",
            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*",
        })
        # Ignore a developer's .env entirely, before any routers bind the singleton.
        from app.core import config

        config.settings = config.Settings(_env_file=None)
        import uvicorn

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", args.port))
            listener.listen(128)
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            app = make_app(url)
            server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=listener.getsockname()[1],
                loop="asyncio", access_log=False, log_level="error",
            ))
            asyncio.run(server.serve(sockets=[listener]))


if __name__ == "__main__":
    main()
