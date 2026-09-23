"""Tests for third-party RSS reader API integration (SPEC §9, Google Reader API standard)."""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import setup_admin

from app.models import Article, Feed, ReaderAccount, Story, StoryState, User
from app.services.readers.greader import GReaderClient, GReaderError, GReaderItem
from app.services.readers.sync import (
    ensure_virtual_feed,
    poll_all_reader_accounts,
    poll_reader_account,
    sync_story_read_state_outbound,
)


# --- Mock GReader Server Responses ---

SAMPLE_STREAM = {
    "id": "user/-/state/com.google/reading-list",
    "title": "All items",
    "continuation": "next_cursor_123",
    "items": [
        {
            "id": "tag:google.com,2005:reader/item/00000001",
            "title": "Ars Article",
            "published": 1700000000,
            "author": "John Doe",
            "canonical": [{"href": "https://arstechnica.com/gadgets/2024/01/new-chip/?utm_source=rss"}],
            "alternate": [{"href": "https://feedly.com/redirect?url=blah"}],
            "summary": {"content": "Summary text here"},
            "origin": {"streamId": "feed/https://arstechnica.com/rss", "title": "Ars Technica"},
            "categories": ["user/-/state/com.google/reading-list"],
        },
        {
            "id": "tag:google.com,2005:reader/item/00000002",
            "title": "The Verge Article",
            "published": 1700000100,
            "author": "Jane Smith",
            "canonical": [],
            "alternate": [{"href": "https://www.theverge.com/2024/review?fbclid=123"}],
            "content": {"content": "Full html content here"},
            "origin": {"streamId": "feed/https://theverge.com/rss", "title": "The Verge"},
            "categories": ["user/-/state/com.google/reading-list", "user/-/state/com.google/read"],
        },
    ],
}


class MockGReaderServer:
    def __init__(self, stream_data: dict[str, Any] | None = None) -> None:
        self.stream_data = stream_data or SAMPLE_STREAM
        self.edit_tags_called: list[dict[str, Any]] = []

    async def fake_http_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        timeout: float = 30.0,
    ) -> tuple[int, bytes, dict[str, str]]:
        if url.endswith("/accounts/ClientLogin"):
            return 200, b"Auth=fake_auth_token_xyz\nSID=s123\nLSID=l456\n", {}

        if url.endswith("/reader/api/0/token"):
            return 200, b"fake_action_token_abc\n", {}

        if "stream/contents" in url:
            return 200, json.dumps(self.stream_data).encode("utf-8"), {"content-type": "application/json"}

        if url.endswith("/reader/api/0/edit-tag"):
            if isinstance(data, list):
                parsed: dict[str, list[str]] = {}
                for k, v in data:
                    parsed.setdefault(k, []).append(v)
                self.edit_tags_called.append(parsed)
            elif isinstance(data, dict):
                self.edit_tags_called.append({k: [v] if isinstance(v, str) else list(v) for k, v in data.items()})
            elif isinstance(data, (bytes, str)):
                s = data.decode("utf-8") if isinstance(data, bytes) else data
                self.edit_tags_called.append(parse_qs(s))
            return 200, b"OK", {}

        return 404, b"Not Found", {}


# --- Client Unit Tests ---


@pytest.mark.anyio
async def test_greader_client_login_and_stream() -> None:
    server = MockGReaderServer()
    client = GReaderClient(
        api_base_url="https://reader.example.com",
        username="alice",
        password="dummy_password",
    )

    with patch("app.services.readers.greader._http_request", side_effect=server.fake_http_request):
        # Login
        auth = await client.authenticate()
        assert auth == "fake_auth_token_xyz"
        assert client.auth_token == "fake_auth_token_xyz"

        # Stream items
        items, continuation = await client.fetch_stream()
        assert len(items) == 2
        assert continuation == "next_cursor_123"

        item1 = items[0]
        assert item1.id == "tag:google.com,2005:reader/item/00000001"
        assert item1.title == "Ars Article"
        # Canonical URL strips tracking query params
        assert item1.url == "https://arstechnica.com/gadgets/2024/01/new-chip/"
        assert item1.origin_feed_title == "Ars Technica"
        assert not item1.is_read

        item2 = items[1]
        assert item2.url == "https://www.theverge.com/2024/review"
        assert item2.origin_feed_title == "The Verge"
        assert item2.is_read

        # Set read state
        await client.set_read_state(item_ids=[item1.id], is_read=True)
        assert len(server.edit_tags_called) == 1
        call = server.edit_tags_called[0]
        assert "user/-/state/com.google/read" in call.get("a", [])
        assert item1.id in call.get("i", [])


# --- Integration & Sync Engine Tests ---


@pytest.mark.anyio
async def test_reader_account_poll_and_virtual_feed(
    db_session: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session() as session:
        user = User(username="testuser", password_hash="hash", is_admin=False)
        session.add(user)
        await session.flush()

        account = ReaderAccount(
            user_id=user.id,
            provider="greader",
            title="My Reader",
            api_base_url="https://reader.example.com",
            username="alice",
            password="dummy_password",
            is_enabled=True,
        )
        session.add(account)
        await session.commit()
        account_id = account.id

    server = MockGReaderServer()

    with patch("app.services.readers.greader._http_request", side_effect=server.fake_http_request),          patch("app.services.readers.sync.fetch_full_text_batch", new=AsyncMock(return_value=[])),          patch("app.services.readers.sync.enqueue_article"):

        async with db_session() as session:
            acc = await session.get(ReaderAccount, account_id)
            assert acc is not None
            res = await poll_reader_account(session, acc)
            assert res["new_articles"] == 2
            assert res["read_synced"] == 0

        async with db_session() as session:
            acc = await session.get(ReaderAccount, account_id)
            assert acc is not None
            assert acc.virtual_feed_id is not None
            assert acc.sync_cursor == "next_cursor_123"
            assert acc.auth_token == "fake_auth_token_xyz"

            # Check virtual feed
            feed = await session.get(Feed, acc.virtual_feed_id)
            assert feed is not None
            assert feed.kind == "reader_api"
            assert feed.title == "My Reader"

            # Check ingested articles
            articles = (
                await session.scalars(
                    select(Article).where(Article.feed_id == feed.id).order_by(Article.id)
                )
            ).all()
            assert len(articles) == 2

            art1 = articles[0]
            assert art1.origin_feed_title == "Ars Technica"
            assert art1.url == "https://arstechnica.com/gadgets/2024/01/new-chip/"

            art2 = articles[1]
            assert art2.origin_feed_title == "The Verge"
            assert art2.url == "https://www.theverge.com/2024/review"


@pytest.mark.anyio
async def test_inbound_read_sync_updated_status(
    db_session: async_sessionmaker[AsyncSession],
) -> None:
    """Test that inbound read items mark single-source stories Read, and multi-source stories 'Updated'."""
    async with db_session() as session:
        user = User(username="testuser", password_hash="hash", is_admin=False)
        session.add(user)
        await session.flush()

        feed = Feed(url="reader:greader:1", kind="reader_api", title="Reader Feed")
        session.add(feed)
        await session.flush()

        account = ReaderAccount(
            user_id=user.id,
            provider="greader",
            title="Reader Feed",
            api_base_url="https://reader.example.com",
            username="alice",
            password="dummy_password",
            is_enabled=True,
            virtual_feed_id=feed.id,
        )
        session.add(account)

        # Story 1: Single article
        story1 = Story(title="Story 1", version=2)
        session.add(story1)
        await session.flush()

        art1 = Article(
            feed_id=feed.id,
            story_id=story1.id,
            guid="tag:google.com,2005:reader/item/00000001",
            url="https://arstechnica.com/gadgets/2024/01/new-chip/",
            title="Ars Article",
            processing_state="clustered",
        )
        session.add(art1)

        # Story 2: Multiple articles (simulating cluster with 2 articles)
        story2 = Story(title="Story 2", version=3)
        session.add(story2)
        await session.flush()

        art2_a = Article(
            feed_id=feed.id,
            story_id=story2.id,
            guid="tag:google.com,2005:reader/item/00000002",
            url="https://www.theverge.com/2024/review",
            title="The Verge Article",
            processing_state="clustered",
        )
        art2_b = Article(
            feed_id=feed.id,
            story_id=story2.id,
            guid="other_guid",
            url="https://techcrunch.com/other",
            title="TechCrunch Article",
            processing_state="clustered",
        )
        session.add_all([art2_a, art2_b])
        await session.commit()
        acc_id = account.id
        s1_id = story1.id
        s2_id = story2.id

    # Stream returns item1 and item2 both marked as read
    stream_data = {
        "id": "user/-/state/com.google/reading-list",
        "continuation": "next_cursor_456",
        "items": [
            {
                "id": "tag:google.com,2005:reader/item/00000001",
                "title": "Ars Article",
                "published": 1700000000,
                "canonical": [{"href": "https://arstechnica.com/gadgets/2024/01/new-chip/"}],
                "categories": ["user/-/state/com.google/read"],
            },
            {
                "id": "tag:google.com,2005:reader/item/00000002",
                "title": "The Verge Article",
                "published": 1700000100,
                "canonical": [{"href": "https://www.theverge.com/2024/review"}],
                "categories": ["user/-/state/com.google/read"],
            },
        ],
    }

    server = MockGReaderServer(stream_data=stream_data)

    with patch("app.services.readers.greader._http_request", side_effect=server.fake_http_request):
        async with db_session() as session:
            acc = await session.get(ReaderAccount, acc_id)
            assert acc is not None
            res = await poll_reader_account(session, acc)
            # Both existing articles should be read_synced
            assert res["read_synced"] == 2

        async with db_session() as session:
            # Check Story 1 state (single article: marked read)
            st1 = await session.scalar(
                select(StoryState).where(StoryState.story_id == s1_id, StoryState.user_id == user.id)
            )
            assert st1 is not None
            assert st1.is_read is True
            # Read version matches current version (not updated)
            assert st1.read_at_version == 2
            assert st1.read_at_version == (await session.get(Story, s1_id)).version

            # Check Story 2 state (multiple articles: marked read but updated)
            st2 = await session.scalar(
                select(StoryState).where(StoryState.story_id == s2_id, StoryState.user_id == user.id)
            )
            assert st2 is not None
            assert st2.is_read is True
            # read_at_version < version => is_read AND updated_since_read = True!
            story2_current = await session.get(Story, s2_id)
            assert st2.read_at_version < story2_current.version


@pytest.mark.anyio
async def test_outbound_read_state_push(
    db_session: async_sessionmaker[AsyncSession],
) -> None:
    """Test pushing read / unread mutations outbound to Google Reader API."""
    async with db_session() as session:
        user = User(username="testuser", password_hash="hash", is_admin=False)
        session.add(user)
        await session.flush()

        feed = Feed(url="reader:greader:1", kind="reader_api", title="Reader Feed")
        session.add(feed)
        await session.flush()

        account = ReaderAccount(
            user_id=user.id,
            provider="greader",
            title="Reader Feed",
            api_base_url="https://reader.example.com",
            username="alice",
            password="dummy_password",
            is_enabled=True,
            virtual_feed_id=feed.id,
        )
        session.add(account)

        story = Story(title="Story Outbound", version=1)
        session.add(story)
        await session.flush()

        art = Article(
            feed_id=feed.id,
            story_id=story.id,
            guid="tag:google.com,2005:reader/item/outbound_01",
            url="https://example.com/outbound",
            title="Outbound Article",
            processing_state="clustered",
        )
        session.add(art)
        await session.commit()
        user_id = user.id
        story_id = story.id

    server = MockGReaderServer()

    with patch("app.services.readers.greader._http_request", side_effect=server.fake_http_request):
        # Mark read outbound
        async with db_session() as session:
            count = await sync_story_read_state_outbound(session, user_id, story_id, is_read=True)
            assert count == 1

        assert len(server.edit_tags_called) == 1
        call = server.edit_tags_called[0]
        assert "user/-/state/com.google/read" in call.get("a", [])
        assert "tag:google.com,2005:reader/item/outbound_01" in call.get("i", [])

        # Mark unread outbound
        server.edit_tags_called.clear()
        async with db_session() as session:
            count = await sync_story_read_state_outbound(session, user_id, story_id, is_read=False)
            assert count == 1

        assert len(server.edit_tags_called) == 1
        call = server.edit_tags_called[0]
        assert "user/-/state/com.google/read" in call.get("r", [])
        assert "tag:google.com,2005:reader/item/outbound_01" in call.get("i", [])


# --- REST API Endpoints Tests ---


@pytest.mark.anyio
async def test_reader_accounts_api_crud(client: AsyncClient) -> None:
    await setup_admin(client)

    server = MockGReaderServer()

    with patch("app.services.readers.greader._http_request", side_effect=server.fake_http_request):
        # Create
        payload = {
            "provider": "greader",
            "title": "My GReader Account",
            "api_base_url": "https://www.inoreader.com",
            "username": "user123",
            "password": "dummy_password",
            "is_enabled": True,
        }
        res = await client.post("/api/reader-accounts", json=payload)
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["id"] > 0
        account_id = data["id"]
        assert data["provider"] == "greader"
        assert data["title"] == "My GReader Account"
        assert data["virtual_feed_id"] is not None

        # List
        res = await client.get("/api/reader-accounts")
        assert res.status_code == 200
        accounts = res.json()
        assert any(a["id"] == account_id for a in accounts)

        # Test probe
        res = await client.post(f"/api/reader-accounts/{account_id}/test")
        assert res.status_code == 200
        probe = res.json()
        assert probe["ok"] is True
        assert probe["items_accessible"] == 2

        # Patch
        res = await client.patch(
            f"/api/reader-accounts/{account_id}",
            json={"title": "Updated Title", "is_enabled": False},
        )
        assert res.status_code == 200
        assert res.json()["title"] == "Updated Title"
        assert res.json()["is_enabled"] is False

        # Delete
        res = await client.delete(f"/api/reader-accounts/{account_id}")
        assert res.status_code == 204

        # List should now be empty
        res = await client.get("/api/reader-accounts")
        assert res.status_code == 200
        assert not any(a["id"] == account_id for a in res.json())
