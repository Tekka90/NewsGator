"""Listen: deterministic text cleaning, has_full_text rules, listen-text endpoint, translation."""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings as env_settings
from app.core.security import make_session_token
from app.models import ActivityEvent, Article, ArticleTranslation, Feed, Story, User, UserFeed
from app.services import listen_text

PROSE_EN = (
    "The council voted on Tuesday to approve the new transport plan after months of debate, "
    "with supporters saying it would cut commuting times across the region."
)
PROSE_FR = (
    "Le conseil a voté mardi en faveur du nouveau plan de transport après des mois de débat, "
    "ses partisans affirmant qu'il réduira les temps de trajet dans la région."
)


def _body(suffix: str = "") -> str:
    return "\n\n".join(f"{PROSE_FR} Paragraphe numéro {n}.{suffix}" for n in range(6))


PROSE_ZH = (
    "市议会周二在数月的辩论之后批准了新的交通计划\uff0c支持者表示这将缩短整个地区的通勤时间。" * 2
)


def test_clean_strips_html_residue_and_entities() -> None:
    raw = (
        "<style>.a{color:red}</style><script>var x = 1;</script>"
        f"<nav><a href='/'>Home</a></nav><p>{PROSE_EN} Fish &amp; chips&nbsp;too.</p>"
        "<footer>All rights</footer>"
    )
    out = listen_text.clean(raw)
    assert out.startswith("The council voted")
    assert "Fish & chips too." in out
    for residue in ("color:red", "var x", "Home", "All rights", "<", "&amp;"):
        assert residue not in out


def test_clean_removes_urls_emails_footnotes_markdown_emoji() -> None:
    raw = (
        f"## Title here\n\n{PROSE_EN} See https://example.com/a?b=1 or www.example.org/x "
        "and write to editor@example.com[1] now.[23] **Bold** and "
        "[a linked phrase](https://example.com/p) 🔥🔥🚀 done.\n\n"
        "![img](https://example.com/i.png)"
    )
    out = listen_text.clean(raw)
    assert "http" not in out and "www." not in out and "@" not in out
    assert "[1]" not in out and "[23]" not in out
    assert "**" not in out and "](" not in out and "#" not in out
    assert "a linked phrase" in out
    assert "🔥" not in out and "🚀" not in out
    assert "img" not in out
    assert "  " not in out


def test_clean_drops_menu_runs_link_dense_and_duplicate_lines() -> None:
    raw = "\n".join(
        [
            "Accueil",
            "Actualités",
            "Sport",
            "Culture",
            "[Lire aussi](https://x.example/1) [Suivre](https://x.example/2)",
            "https://x.example/only-a-link",
            PROSE_FR,
            PROSE_FR,
            "Sous-titre court",
            PROSE_FR + " Suite.",
        ]
    )
    out = listen_text.clean(raw)
    paragraphs = out.split("\n\n")
    assert paragraphs[0] == PROSE_FR
    assert out.count(PROSE_FR) == 2  # duplicate of line 1 collapsed; the longer variant kept
    assert "Accueil" not in out and "Culture" not in out and "Lire aussi" not in out
    assert "Sous-titre court" in paragraphs  # short heading adjacent to prose is kept


def test_clean_mixed_languages_and_scripts() -> None:
    out = listen_text.clean(f"{PROSE_EN}\n{PROSE_ZH}\n{PROSE_FR}")
    assert out.split("\n\n") == [PROSE_EN, PROSE_ZH, PROSE_FR]


def test_clean_empty_and_symbol_only() -> None:
    assert listen_text.clean(None) == ""
    assert listen_text.clean("***\n---\n• • •\n\n  ") == ""


def test_usable_text_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "fulltext_min_chars", 100)
    assert listen_text.usable_text("full", PROSE_EN)
    assert not listen_text.usable_text("partial", PROSE_EN)
    assert not listen_text.usable_text("full", "Short text.")
    assert not listen_text.usable_text("full", None)
    # Mostly residue: cleaned/raw ratio too low
    noisy = PROSE_EN + "\n" + "\n".join(f"[menu {n}](https://a.example/{n})" for n in range(200))
    assert not listen_text.usable_text("full", noisy)


async def _setup(db_session, *, text: str | None = None, status: str = "full", lang: str = "fr"):
    async with db_session() as s:
        feed = Feed(url="https://example.com/rss", title="Feed")
        s.add(feed)
        story = Story(title="T", summary="S", category="General", language="en")
        s.add(story)
        await s.flush()
        article = Article(
            feed_id=feed.id,
            guid="g1",
            url="https://example.com/a",
            title="A",
            story_id=story.id,
            processing_state="clustered",
            full_text=text if text is not None else _body(),
            content_status=status,
            language=lang,
        )
        s.add(article)
        owner = User(username="owner", password_hash="x", is_admin=True, summary_language="en")
        other = User(username="other", password_hash="x", is_admin=False)
        s.add_all([owner, other])
        await s.flush()
        s.add(UserFeed(user_id=owner.id, feed_id=feed.id))
        await s.commit()
        return article.id, story.id, owner.id, other.id


def _auth(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_session_token(user_id)}"}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "summary_language", "en")
    monkeypatch.setattr(env_settings, "fulltext_min_chars", 400)


async def test_requires_auth(client: AsyncClient, db_session) -> None:
    article_id, *_ = await _setup(db_session)
    r = await client.get(f"/api/stories/articles/{article_id}/listen-text")
    assert r.status_code == 401


async def test_original_returns_cleaned_text(client: AsyncClient, db_session) -> None:
    article_id, _, owner, _ = await _setup(db_session)
    r = await client.get(f"/api/stories/articles/{article_id}/listen-text", headers=_auth(owner))
    assert r.status_code == 200
    body = r.json()
    assert body["article_id"] == article_id
    assert body["translated"] is False
    assert body["language"] == "fr"
    assert body["content_status"] == "full"
    assert body["text"].startswith("Le conseil a voté")


async def test_isolation_and_missing_404(client: AsyncClient, db_session) -> None:
    article_id, _, _, other = await _setup(db_session)
    r = await client.get(f"/api/stories/articles/{article_id}/listen-text", headers=_auth(other))
    assert r.status_code == 404
    r = await client.get("/api/stories/articles/9999/listen-text", headers=_auth(other))
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("text", "status"),
    [(None, "partial"), ("too short", "full"), (_body(), "partial")],
)
async def test_unusable_text_is_409(client: AsyncClient, db_session, text, status) -> None:
    article_id, _, owner, _ = await _setup(
        db_session, text=text if text is not None else "x", status=status
    )
    r = await client.get(f"/api/stories/articles/{article_id}/listen-text", headers=_auth(owner))
    assert r.status_code == 409


async def test_bad_lang_is_422(client: AsyncClient, db_session) -> None:
    article_id, _, owner, _ = await _setup(db_session)
    r = await client.get(
        f"/api/stories/articles/{article_id}/listen-text?lang=xx", headers=_auth(owner)
    )
    assert r.status_code == 422


async def test_summary_same_language_is_not_translated(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    article_id, _, owner, _ = await _setup(db_session, lang="en")
    mock = AsyncMock()
    monkeypatch.setattr(listen_text, "_translate_chunk", mock)
    r = await client.get(
        f"/api/stories/articles/{article_id}/listen-text?lang=summary", headers=_auth(owner)
    )
    assert r.status_code == 200
    assert r.json()["translated"] is False
    mock.assert_not_called()


async def test_summary_translates_caches_and_invalidates(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    article_id, _, owner, _ = await _setup(db_session)
    mock = AsyncMock(return_value=("Translated text.", 42))
    monkeypatch.setattr(listen_text, "_translate_chunk", mock)
    url = f"/api/stories/articles/{article_id}/listen-text?lang=summary"

    r = await client.get(url, headers=_auth(owner))
    assert r.status_code == 200
    body = r.json()
    assert body["translated"] is True and body["language"] == "en"
    assert body["text"] == "Translated text."
    calls = mock.call_count
    assert calls >= 1

    r = await client.get(url, headers=_auth(owner))  # cache hit
    assert r.json()["text"] == "Translated text."
    assert mock.call_count == calls

    async with db_session() as s:
        article = await s.get(Article, article_id)
        assert article is not None
        article.full_text = _body(" Nouveau.")
        await s.commit()
    mock.return_value = ("Fresh text.", 10)
    r = await client.get(url, headers=_auth(owner))  # source changed → re-translated
    assert r.json()["text"] == "Fresh text."
    assert mock.call_count > calls

    async with db_session() as s:
        rows = (await s.scalars(select(ArticleTranslation))).all()
        assert len(rows) == 1 and rows[0].text == "Fresh text."
        actions = {e.action for e in (await s.scalars(select(ActivityEvent))).all()}
        assert {"article_translate_start", "article_translated"} <= actions


async def test_translation_failure_is_502_and_logged(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import llm_client

    article_id, _, owner, _ = await _setup(db_session)
    monkeypatch.setattr(
        listen_text, "_translate_chunk", AsyncMock(side_effect=llm_client.LLMError("boom"))
    )
    r = await client.get(
        f"/api/stories/articles/{article_id}/listen-text?lang=summary", headers=_auth(owner)
    )
    assert r.status_code == 502
    async with db_session() as s:
        actions = {e.action for e in (await s.scalars(select(ActivityEvent))).all()}
        assert "article_translate_error" in actions
        assert (await s.scalars(select(ArticleTranslation))).first() is None


async def test_story_detail_exposes_full_text_fields(client: AsyncClient, db_session) -> None:
    article_id, story_id, owner, _ = await _setup(db_session)
    r = await client.get(f"/api/stories/{story_id}", headers=_auth(owner))
    art = r.json()["articles"][0]
    assert art["id"] == article_id
    assert art["has_full_text"] is True
    assert art["char_count"] == len(listen_text.clean(_body()))

    async with db_session() as s:
        article = await s.get(Article, article_id)
        assert article is not None
        article.content_status = "partial"
        await s.commit()
    art = (await client.get(f"/api/stories/{story_id}", headers=_auth(owner))).json()["articles"][0]
    assert art["has_full_text"] is False
    assert art["char_count"] > 0
