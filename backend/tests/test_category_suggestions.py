"""Category-suggestion aggregation + admin API (services/category_suggestions.py)."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import setup_admin

from app.models import Article, CategoryProposalDismissal, CategorySuggestion, Feed
from app.services import category_suggestions as cs


def test_normalize() -> None:
    assert cs.normalize("Apple") == "apple"
    assert cs.normalize(" Apple. ") == "apple"
    assert cs.normalize("Apple Inc.") == "apple inc"
    assert cs.normalize("  Multiple   Spaces  ") == "multiple spaces"


async def _article(factory: async_sessionmaker[AsyncSession], title: str = "A title") -> Article:
    async with factory() as s:
        feed = Feed(url=f"https://f{id(title)}.example.com/rss")
        s.add(feed)
        await s.flush()
        article = Article(feed_id=feed.id, guid="g", url="https://x.example.com/a", title=title)
        s.add(article)
        await s.commit()
        return article


async def test_record_suggestion_skips_blank_and_taxonomy_matches(db_session) -> None:
    article = await _article(db_session)
    async with db_session() as s:
        await cs.record_suggestion(s, article.id, "  ", ["Tech"])
        await cs.record_suggestion(s, article.id, "tech", ["Tech"])
        await cs.record_suggestion(s, article.id, "Apple", ["Tech"])
        await s.commit()

    async with db_session() as s:
        rows = (await s.scalars(select(CategorySuggestion))).all()
        assert len(rows) == 1
        assert rows[0].raw_text == "Apple"


async def test_list_proposals_threshold_and_grouping(db_session) -> None:
    articles = [await _article(db_session, f"Article {i}") for i in range(5)]
    async with db_session() as s:
        # 4 distinct articles suggest variants of "Apple" (below default threshold of 5)
        for a, raw in zip(articles[:4], ["Apple", "apple.", " Apple ", "APPLE"], strict=True):
            s.add(CategorySuggestion(article_id=a.id, raw_text=raw, normalized_text="apple"))
        await s.commit()

    async with db_session() as s:
        proposals = await cs.list_proposals(s, min_articles=5, window_days=30)
        assert proposals == []

        proposals = await cs.list_proposals(s, min_articles=4, window_days=30)
        assert len(proposals) == 1
        assert proposals[0].article_count == 4
        assert proposals[0].label == "Apple"  # most frequent raw form


async def test_list_proposals_ignores_old_and_dismissed(db_session) -> None:
    articles = [await _article(db_session, f"Article {i}") for i in range(3)]
    old = datetime.now(UTC) - timedelta(days=90)
    async with db_session() as s:
        for a in articles:
            s.add(
                CategorySuggestion(
                    article_id=a.id,
                    raw_text="Old topic",
                    normalized_text="old topic",
                    created_at=old,
                )
            )
        await s.commit()

    async with db_session() as s:
        proposals = await cs.list_proposals(s, min_articles=1, window_days=30)
        assert proposals == []  # outside the window

    async with db_session() as s:
        proposals = await cs.list_proposals(s, min_articles=1, window_days=365)
        assert len(proposals) == 1

    async with db_session() as s:
        await cs.dismiss(s, "old topic")

    async with db_session() as s:
        proposals = await cs.list_proposals(s, min_articles=1, window_days=365)
        assert proposals == []
        assert await s.get(CategoryProposalDismissal, "old topic") is not None


async def test_clear_suggestions(db_session) -> None:
    article = await _article(db_session)
    async with db_session() as s:
        s.add(CategorySuggestion(article_id=article.id, raw_text="Apple", normalized_text="apple"))
        await s.commit()

    async with db_session() as s:
        await cs.clear_suggestions(s, "apple")

    async with db_session() as s:
        assert (await s.scalars(select(CategorySuggestion))).all() == []


# --- admin API ---


async def test_suggestions_api_list_accept_dismiss(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    articles = [await _article(db_session, f"Article {i}") for i in range(5)]
    async with db_session() as s:
        for a in articles:
            s.add(CategorySuggestion(article_id=a.id, raw_text="Apple", normalized_text="apple"))
        await s.commit()

    r = await client.get("/api/categories/suggestions")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["label"] == "Apple"
    assert body[0]["article_count"] == 5

    # Accept: creates the category and clears the suggestion log
    r = await client.post(
        "/api/categories/suggestions/accept",
        json={"normalized_text": "apple", "label": "Apple"},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Apple"

    r = await client.get("/api/categories")
    assert "Apple" in [c["name"] for c in r.json()]

    r = await client.get("/api/categories/suggestions")
    assert r.json() == []  # log cleared, and "apple" now matches the taxonomy


async def test_suggestions_api_dismiss(client: AsyncClient, db_session) -> None:
    await setup_admin(client)
    articles = [await _article(db_session, f"Article {i}") for i in range(5)]
    async with db_session() as s:
        for a in articles:
            s.add(CategorySuggestion(article_id=a.id, raw_text="Foo", normalized_text="foo"))
        await s.commit()

    r = await client.post(
        "/api/categories/suggestions/dismiss", json={"normalized_text": "foo", "label": ""}
    )
    assert r.status_code == 204

    r = await client.get("/api/categories/suggestions")
    assert r.json() == []
