"""Real HTTP Apple contracts; no running installation or external services are used.

Run from backend: .venv/bin/python -m pytest tests/test_apple_contract.py -q
TemporaryDirectory is explicitly rooted under tests, never the system temp directory.
Each test gets a fresh uvicorn process, database, signing key, and vector store.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest

TESTS = Path(__file__).resolve().parent
ADMIN = {"username": "apple-admin", "password": "apple-admin-pass"}
READER = {"username": "apple-reader", "password": "apple-reader-pass"}
STAMP = "2026-09-01T12:00:00Z"


@pytest.fixture
def live() -> Iterator[httpx.Client]:
    with TemporaryDirectory(prefix=".apple-contract-", dir=TESTS) as directory:
        log_path = Path(directory) / "server.log"
        ignored_database = Path(directory) / "must-not-be-used.db"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, str(TESTS / "apple_smoke_server.py"), "--port", "0"],
                cwd=TESTS.parent,
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "DATABASE_URL": f"sqlite+aiosqlite:///{ignored_database}",
                    "ENVIRONMENT": "production",
                    "VECTOR_BACKEND": "qdrant",
                    "QDRANT_URL": "https://must-not-be-contacted.invalid",
                },
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 30
                url = None
                while time.monotonic() < deadline and proc.poll() is None:
                    # A separate reader must not move the child's shared stdout offset.
                    for line in log_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("http://127.0.0.1:"):
                            url = line
                            break
                    if url:
                        try:
                            response = httpx.get(
                                url + "/api/auth/setup-needed", timeout=0.5, trust_env=False
                            )
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                    time.sleep(0.05)
                else:
                    pytest.fail(
                        "Isolated HTTP server did not start:\n"
                        + log_path.read_text(encoding="utf-8")
                    )
                assert url
                assert not ignored_database.exists()
                with httpx.Client(base_url=url + "/", timeout=10, trust_env=False) as client:
                    yield client
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                assert not ignored_database.exists()


def login(client: httpx.Client, credentials: dict = READER) -> dict:
    response = client.post("/api/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "id", "username", "is_admin", "summary_language", "story_sort",
        "story_order", "story_filter", "token",
    }
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {body['token']}"
    return body


def test_auth_bearer_preferences_and_invalid_session(live: httpx.Client) -> None:
    assert live.get("/api/auth/setup-needed").json() == {"setup_needed": False}
    assert live.get("/api/stories").status_code == 401
    assert live.post("/api/auth/login", json={**READER, "password": "wrong"}).status_code == 401
    user = login(live)
    assert user["is_admin"] is False
    preferences = {"story_filter": "updated", "story_sort": "sources", "story_order": "desc"}
    response = live.patch("/api/auth/me", json=preferences)
    assert response.status_code == 200
    assert preferences.items() <= response.json().items()
    assert "token" not in response.json()
    token = live.post("/api/auth/session-token").json()["token"]
    live.headers.pop("Authorization")
    assert live.get("/api/auth/me", params={"token": token}).json()["username"] == READER[
        "username"
    ]
    assert live.post("/api/auth/logout").status_code == 204
    assert live.get("/api/auth/me", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_story_list_detail_revision_and_feed_options(live: httpx.Client) -> None:
    login(live)
    stories = live.get("/api/stories").json()
    assert [story["id"] for story in stories] == [1, 2]
    assert stories[0] == {
        "id": 1, "title": "Apple contract RSS story", "summary": "Updated RSS facts.",
        "category": "Tech", "image_url": None, "version": 2, "is_frozen": False,
        "source_count": 1, "source_hosts": ["127.0.0.1"], "published_at": STAMP,
        "last_updated_at": STAMP, "is_read": True, "updated_since_read": True,
        "readeck_bookmark_id": None,
    }
    detail = live.get("/api/stories/1").json()
    assert set(detail) == {
        "id", "title", "summary", "category", "image_url", "version", "is_frozen",
        "first_seen_at", "last_updated_at", "published_at", "is_read",
        "updated_since_read", "articles", "revisions", "readeck_bookmark_id",
    }
    assert detail["revisions"] == [
        {"version": 1, "summary": "Original RSS facts.", "created_at": STAMP},
        {"version": 2, "summary": "Updated RSS facts.", "created_at": STAMP},
    ]
    assert detail["articles"] == [{
        "id": 1, "title": "RSS source", "url": f"{live.base_url}fixture/source/1",
        "image_url": None, "language": "en", "summary": "Updated RSS facts.",
        "content_status": "full", "content_warning": None, "published_at": STAMP,
        "feed_id": 1, "feed_title": "Apple RSS", "feed_url": f"{live.base_url}fixture/rss",
    }]
    options = live.get("/api/stories/feed-options").json()
    assert options == [
        {"id": 2, "title": "Apple Mail", "kind": "mail",
         "url": "newsletter:newsletter@example.invalid",
         "sender_email": "newsletter@example.invalid"},
        {"id": 1, "title": "Apple RSS", "kind": "rss",
         "url": f"{live.base_url}fixture/rss", "sender_email": None},
    ]
    assert [s["id"] for s in live.get("/api/stories?feed=2&category=Science").json()] == [2]
    assert live.get("/api/stories?filter=invalid").status_code == 422
    assert live.get("/api/stories/999").json() == {"detail": "Story not found"}
    diff = live.get("/api/stories/1/diff?from=1")
    assert diff.status_code == 200
    assert diff.json() == {
        "from_version": 1,
        "changes": [{"version": 2, "summary": "Updated RSS facts.", "at": STAMP}],
    }


def test_read_state_is_per_user_and_never_reverts_on_update(live: httpx.Client) -> None:
    login(live)
    assert [s["id"] for s in live.get("/api/stories?filter=updated").json()] == [1]
    assert [s["id"] for s in live.get("/api/stories?filter=unread").json()] == [2]
    response = live.post("/api/stories/1/read")
    assert response.status_code == 204 and response.content == b""
    detail = live.get("/api/stories/1").json()
    assert detail["is_read"] is True and detail["updated_since_read"] is False
    login(live, ADMIN)
    assert live.get("/api/stories/1").json()["is_read"] is False
    login(live)
    assert live.post("/api/stories/1/unread").status_code == 204
    assert live.get("/api/stories/1").json()["is_read"] is False


@pytest.mark.parametrize("path", [
    "/api/feeds", "/api/categories", "/api/categories/suggestions",
    "/api/users", "/api/settings", "/api/usage/summary",
    "/api/usage/daily", "/api/usage/by-feed",
])
def test_nonadmin_administration_is_forbidden(live: httpx.Client, path: str) -> None:
    login(live)
    response = live.get(path)
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin only"}


def test_chat_real_retrieval_history_and_usage_with_fake_provider(live: httpx.Client) -> None:
    login(live)
    assert live.get("/api/chat/history").json() == []
    response = live.post("/api/chat", json={"question": "What changed?"})
    assert response.status_code == 200, response.text
    answer = response.json()
    assert set(answer) == {"answer", "stories", "latency_ms"}
    assert answer["answer"] == "Deterministic fixture answer [Story 1]."
    assert answer["latency_ms"] == 7
    assert answer["stories"][0] == {
        "id": 1, "title": "Apple contract RSS story", "category": "Tech",
        "image_url": None, "last_updated_at": STAMP,
        "source_hosts": [str(live.base_url).split("/")[2]],
        "similarity": 1.0, "cited": True,
    }
    history = live.get("/api/chat/history").json()
    assert history == [
        {"role": "user", "content": "What changed?", "stories": [], "latency_ms": 0},
        {"role": "assistant", "content": answer["answer"],
         "stories": answer["stories"], "latency_ms": 7},
    ]
    login(live, ADMIN)
    assert live.get("/api/chat/history").json() == []
    usage = live.get("/api/usage/summary").json()
    assert set(usage) == {"period", "totals", "by_kind", "by_model"}
    assert usage["totals"]["calls"] == 2
    assert {row["kind"] for row in usage["by_kind"]} == {"chat_embed", "chat_answer"}
    assert set(live.get("/api/usage/daily").json()) == {"days", "rows"}
    assert set(live.get("/api/usage/by-feed").json()) == {"feeds"}
    login(live)
    assert live.delete("/api/chat/history").status_code == 204
    assert live.get("/api/chat/history").json() == []


def test_admin_categories_feeds_and_local_rss_ingestion(live: httpx.Client) -> None:
    login(live, ADMIN)
    feeds = live.get("/api/feeds").json()
    assert len(feeds) == 2
    assert set(feeds[0]) == {
        "id", "url", "kind", "sender_email", "title", "is_enabled", "poll_interval_min",
        "backfill_days", "last_fetched_at", "last_error", "consecutive_failures",
        "fetch_fulltext", "story_count", "unread_story_count", "email_count",
    }
    assert all(feed["story_count"] == feed["unread_story_count"] == 1 for feed in feeds)
    created = live.post("/api/categories", json={"name": "Apple custom"})
    assert created.status_code == 201
    category = created.json()
    assert set(category) == {"id", "name"}
    assert live.patch(f"/api/categories/{category['id']}", json={"name": "Renamed"}).json() == {
        "id": category["id"], "name": "Renamed",
    }
    assert live.delete(f"/api/categories/{category['id']}").status_code == 204
    refreshed = live.post("/api/feeds/1/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json() == {"new_articles": 1}
    assert live.post("/api/feeds/1/refresh").json() == {"new_articles": 0}
    pipeline = live.get("/api/activity/pipeline").json()
    assert any(row["title"] == "Locally ingested RSS item" for row in pipeline["rows"])
    assert live.post("/api/feeds/2/refresh").status_code == 400
    exported = live.get("/api/feeds/export-opml")
    assert exported.status_code == 200 and "newsletter:" not in exported.text
    assert str(live.base_url) + "fixture/rss" in exported.text


def test_mail_accounts_are_private_and_passwords_write_only(live: httpx.Client) -> None:
    login(live)
    created = live.post("/api/mail-accounts", json={
        "host": "127.0.0.1", "port": 1993, "username": "fixture-mail",
        "password": "fixture-mail-pass", "folder": "News", "use_ssl": False,
    })
    assert created.status_code == 201
    account = created.json()
    assert "password" not in account
    path = f"/api/mail-accounts/{account['id']}"
    assert live.post(path + "/test").json() == {"ok": True, "errors": [], "folder": "News"}
    assert live.post(path + "/poll").json() == {"found": 0, "processing": False}
    login(live, ADMIN)
    assert live.get("/api/mail-accounts").json() == []
    assert live.patch(path, json={"folder": "Other"}).status_code == 404
    login(live)
    assert live.delete(path).status_code == 204


def test_added_local_feed_reaches_a_real_story_with_fake_inference(live: httpx.Client) -> None:
    login(live, ADMIN)
    created = live.post("/api/feeds", json={
        "url": f"{live.base_url}fixture/rss?client=swift",
        "title": "Swift HTTP feed", "fetch_fulltext": False, "backfill_days": 0,
    })
    assert created.status_code == 201, created.text
    feed_id = created.json()["id"]
    assert live.post(f"/api/feeds/{feed_id}/refresh").json() == {"new_articles": 1}
    deadline = time.monotonic() + 5
    stories = []
    while time.monotonic() < deadline:
        stories = live.get("/api/stories", params={"feed": feed_id}).json()
        if stories:
            break
        time.sleep(0.05)
    assert len(stories) == 1, live.get("/api/activity/recent").text
    story = stories[0]
    assert story["id"] not in [1, 2]
    assert story["title"] == "Locally ingested RSS story"
    assert story["summary"] == "Facts fetched through actual local HTTP RSS ingestion."
    assert story["category"] == "Tech"
    assert story["version"] == 1
    detail = live.get(f"/api/stories/{story['id']}").json()
    assert detail["articles"][0]["feed_id"] == feed_id
    assert detail["articles"][0]["url"] == f"{live.base_url}fixture/source/3?client=swift"
    assert live.get("/fixture/source/2").status_code == 200
    assert live.post(f"/api/feeds/{feed_id}/refresh").json() == {"new_articles": 0}


def test_rss_clients_do_not_cross_deduplicate(live: httpx.Client) -> None:
    login(live, ADMIN)
    for client in ("first-run", "second-run"):
        created = live.post("/api/feeds", json={
            "url": f"{live.base_url}fixture/rss?client={client}",
            "title": client, "fetch_fulltext": False, "backfill_days": 0,
        })
        assert created.status_code == 201, created.text
        feed_id = created.json()["id"]
        assert live.post(f"/api/feeds/{feed_id}/refresh").json() == {"new_articles": 1}
        assert live.post(f"/api/feeds/{feed_id}/refresh").json() == {"new_articles": 0}


def test_stream_query_auth_and_actual_chat_activity(live: httpx.Client) -> None:
    token = login(live)["token"]
    with httpx.Client(base_url=live.base_url, timeout=10, trust_env=False) as stream_client:
        with stream_client.stream(
            "GET", "/api/activity/stream", params={"token": token}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.iter_lines()
            assert next(lines) == "event: hello"
            assert json.loads(next(lines).removeprefix("data: ")) == {"llm_queue_depth": 0}
            assert live.post("/api/chat", json={"question": "Stream?"}).status_code == 200
            for line in lines:
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    assert event["component"] == "chat"
                    assert event["action"] == "chat_query"
                    assert event["detail"]["phase"] == "start"
                    break
    rss = live.get("/api/feed.xml", params={"token": token})
    assert rss.status_code == 200 and "story:1" in rss.text
    assert set(live.get("/api/activity/recent").json()) == {"events", "llm_queue_depth"}


def test_share_and_fake_readeck_preserve_real_response_contract(live: httpx.Client) -> None:
    login(live)
    shared = live.post("/api/stories/1/share", json={})
    assert shared.status_code == 200, shared.text
    assert set(shared.json()) == {"title", "text", "url", "language", "translated", "latency_ms"}
    assert shared.json()["translated"] is False
    assert shared.json()["url"] == f"{live.base_url}fixture/source/1"
    saved = live.post("/api/stories/1/readeck")
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "bookmark_id": "fixture-bookmark-1", "href": f"{live.base_url}fixture/bookmark/1",
        "latency_ms": 3,
    }
    assert live.get("/api/stories/1").json()["readeck_bookmark_id"] == "fixture-bookmark-1"


def test_boolean_settings_roundtrip_updates_actual_runtime(live: httpx.Client) -> None:
    login(live, ADMIN)
    before = live.get("/api/settings").json()
    assert "llm_trace_enabled" not in before["env_locked"]
    for enabled in [False, True]:
        updated = live.patch(
            "/api/settings", json={"values": {"llm_trace_enabled": enabled}}
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["values"]["llm_trace_enabled"] is enabled
        assert live.get("/api/settings").json()["values"]["llm_trace_enabled"] is enabled
        assert live.get("/api/activity/llm").json()["enabled"] is enabled
    cleared = live.patch("/api/settings", json={"values": {"llm_trace_enabled": None}})
    assert cleared.status_code == 200
    assert "llm_trace_enabled" not in cleared.json()["overridden"]
    original = before["values"]["llm_trace_enabled"]
    assert cleared.json()["values"]["llm_trace_enabled"] is original
    assert live.get("/api/activity/llm").json()["enabled"] is original
