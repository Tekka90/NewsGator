"""Google Reader client edge cases, login diagnosis and reader-account API errors."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import setup_admin

from app.models import ReaderAccount
from app.services.readers import diagnose, greader
from app.services.readers.greader import (
    GReaderAuthError,
    GReaderClient,
    GReaderError,
    html_to_text,
)

Response = tuple[int, bytes, dict[str, str]]


class Script:
    """Scripted stand-in for greader._http_request: handler(method, url, kw) -> response."""

    def __init__(self, handler: Callable[[str, str, dict[str, Any]], Response]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, method: str, url: str, **kw: Any) -> Response:
        self.calls.append((method, url, kw))
        return self.handler(method, url, kw)


def install(monkeypatch: pytest.MonkeyPatch, handler: Callable[..., Response]) -> Script:
    script = Script(handler)
    monkeypatch.setattr(greader, "_http_request", script)
    return script


# --- html_to_text -------------------------------------------------------------


def test_html_to_text_plain_passthrough_and_block_breaks() -> None:
    assert html_to_text("  just text ") == "just text"
    text = html_to_text("<p>One&nbsp;two</p><script>x()</script><p>Three</p><style>p{}</style>")
    assert text == "One two\nThree"


def test_html_to_text_unparseable_falls_back_to_regex() -> None:
    assert html_to_text("<!-- -->") == ""


# --- authentication -----------------------------------------------------------


def test_auth_headers_prefix_rules() -> None:
    assert GReaderClient("https://x")._auth_headers() == {}
    assert GReaderClient("https://x", auth_token="abc")._auth_headers() == {
        "Authorization": "GoogleLogin auth=abc"
    }
    assert GReaderClient("https://x", auth_token="Bearer t")._auth_headers() == {
        "Authorization": "Bearer t"
    }
    assert GReaderClient("https://x/", auth_token="GoogleLogin auth=z")._auth_headers() == {
        "Authorization": "GoogleLogin auth=z"
    }
    assert GReaderClient("https://x/api/")._resolve_url("/a/b") == "https://x/api/a/b"


async def test_authenticate_requires_credentials() -> None:
    with pytest.raises(GReaderAuthError, match="required"):
        await GReaderClient("https://x").authenticate()


async def test_authenticate_login_success_and_root_fallback(monkeypatch) -> None:
    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        if url == "https://h.example/sub/accounts/ClientLogin":
            return 404, b"", {}
        assert url == "https://h.example/accounts/ClientLogin"
        assert kw["data"]["Email"] == "u" and kw["data"]["Passwd"] == "p"
        return 200, b"SID=x\nAuth=tok123\n", {}

    script = install(monkeypatch, handler)
    client = GReaderClient("https://h.example/sub", "u", "p")
    assert await client.authenticate() == "tok123"
    assert client.auth_token == "tok123"
    assert len(script.calls) == 2


@pytest.mark.parametrize(
    ("status", "body", "exc", "msg"),
    [
        (401, b"", GReaderAuthError, "Invalid credentials"),
        (403, b"", GReaderAuthError, "Invalid credentials"),
        (500, b"oops", GReaderError, "HTTP 500: oops"),
        (200, b"SID=only", GReaderAuthError, "did not contain"),
    ],
)
async def test_authenticate_failures(monkeypatch, status, body, exc, msg) -> None:
    install(monkeypatch, lambda *_a: (status, body, {}))
    with pytest.raises(exc, match=msg):
        await GReaderClient("https://h.example", "u", "p").authenticate()


async def test_authenticate_stale_token_relogs_in_or_raises(monkeypatch) -> None:
    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        if url.endswith("/reader/api/0/token"):
            auth = (kw.get("headers") or {}).get("Authorization", "")
            return (200, b"act", {}) if auth.endswith("fresh") else (401, b"", {})
        return 200, b"Auth=fresh", {}

    install(monkeypatch, handler)
    client = GReaderClient("https://h.example", "u", "p", auth_token="stale")
    assert await client.authenticate() == "fresh"

    install(monkeypatch, lambda *_a: (401, b"", {}))
    with pytest.raises(GReaderAuthError):
        await GReaderClient("https://h.example", auth_token="stale").authenticate()


async def test_authenticate_with_valid_token_returns_it(monkeypatch) -> None:
    install(monkeypatch, lambda *_a: (200, b"act", {}))
    assert await GReaderClient("https://h", auth_token="good").authenticate() == "good"


# --- get_token ----------------------------------------------------------------


async def test_get_token_retries_after_reauth_and_reports_errors(monkeypatch) -> None:
    state = {"authed": False}

    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        if url.endswith("ClientLogin"):
            state["authed"] = True
            return 200, b"Auth=new", {}
        return (200, b" tok \n", {}) if state["authed"] else (403, b"", {})

    install(monkeypatch, handler)
    client = GReaderClient("https://h", "u", "p")
    assert await client.get_token() == "tok"
    assert client._action_token == "tok"

    install(monkeypatch, lambda *_a: (403, b"", {}))
    with pytest.raises(GReaderAuthError, match="Unauthorized token"):
        await GReaderClient("https://h").get_token()

    install(monkeypatch, lambda *_a: (500, b"", {}))
    with pytest.raises(GReaderError, match="HTTP 500"):
        await GReaderClient("https://h").get_token()


# --- fetch_stream -------------------------------------------------------------


def _stream(items: list[dict[str, Any]], cont: str | None = None) -> bytes:
    return json.dumps({"items": items, "continuation": cont}).encode()


async def test_fetch_stream_parses_item_variants_and_params(monkeypatch) -> None:
    items = [
        {  # canonical link, enclosure image, summary content, read tag
            "id": "i1",
            "title": "One",
            "canonical": [{"href": "https://a.example/p?utm_source=x"}],
            "published": 1_700_000_000,
            "crawlTimeMsec": "1700000100000",
            "origin": {"title": "Feed A"},
            "categories": ["user/-/state/com.google/read", "user/-/label/x"],
            "enclosure": [{"href": "https://a.example/i.jpg", "type": "image/jpeg"}],
            "summary": {"content": "<p>Hello</p>"},
        },
        {  # alternate link, content beats summary, bad published → crawl time
            "id": "i2",
            "title": "Two",
            "alternate": [{"href": "https://b.example/q"}],
            "published": "garbage",
            "crawlTimeMsec": "1700000200000",
            "content": {"content": "body"},
            "summary": {"content": "ignored"},
            "categories": ["user/-/state/com.google/reading-list"],
        },
        {"id": "i3"},  # no link at all → id is the url; no dates
        {"title": "no id"},  # skipped
    ]
    script = install(monkeypatch, lambda *_a, **_k: (200, _stream(items, "next"), {}))
    client = GReaderClient("https://h", auth_token="t")
    got, cont = await client.fetch_stream(
        continuation="c1", limit=7, exclude_read=True, oldest_ts=123, oldest_first=True
    )
    params = script.calls[0][2]["params"]
    assert params == {
        "output": "json",
        "n": "7",
        "c": "c1",
        "xt": "user/-/state/com.google/read",
        "r": "o",
        "ot": "123",
    }
    assert cont == "next" and [i.id for i in got] == ["i1", "i2", "i3"]
    one, two, three = got
    assert one.url == "https://a.example/p" and one.is_read is True
    assert one.image_url == "https://a.example/i.jpg" and one.raw_content == "Hello"
    assert one.origin_feed_title == "Feed A"
    assert one.published_at is not None and one.published_at.year == 2023
    assert one.crawled_at is not None and one.crawled_at > one.published_at
    assert two.is_read is False and two.raw_content == "body"
    assert two.published_at == two.crawled_at
    assert three.url.endswith("i3") and three.published_at is None and three.title == ""


async def test_fetch_stream_logs_in_first_and_retries_on_401(monkeypatch) -> None:
    state = {"stream_calls": 0}

    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        if url.endswith("ClientLogin"):
            return 200, b"Auth=tok", {}
        if not url.endswith("contents/user/-/state/com.google/reading-list"):
            return 200, b"act", {}
        state["stream_calls"] += 1
        return (401, b"", {}) if state["stream_calls"] == 1 else (200, _stream([]), {})

    install(monkeypatch, handler)
    client = GReaderClient("https://h", "u", "p")
    assert await client.fetch_stream() == ([], None)
    assert state["stream_calls"] == 2


@pytest.mark.parametrize(("status", "exc"), [(401, GReaderAuthError), (503, GReaderError)])
async def test_fetch_stream_errors(monkeypatch, status, exc) -> None:
    install(monkeypatch, lambda *_a, **_k: (status, b"", {}))
    with pytest.raises(exc):
        await GReaderClient("https://h", auth_token="t").fetch_stream()


# --- unread ids / read state --------------------------------------------------


async def test_get_unread_item_ids_filters_malformed_refs(monkeypatch) -> None:
    body = json.dumps({"itemRefs": [{"id": 5}, {"id": "6"}, {"x": 1}, "bad"]}).encode()
    install(monkeypatch, lambda *_a, **_k: (200, body, {}))
    assert await GReaderClient("https://h", auth_token="t").get_unread_item_ids() == {"5", "6"}


async def test_get_unread_item_ids_errors_and_reauth(monkeypatch) -> None:
    install(monkeypatch, lambda *_a, **_k: (401, b"", {}))
    with pytest.raises(GReaderAuthError):
        await GReaderClient("https://h", auth_token="t").get_unread_item_ids()
    install(monkeypatch, lambda *_a, **_k: (502, b"", {}))
    with pytest.raises(GReaderError, match="502"):
        await GReaderClient("https://h", auth_token="t").get_unread_item_ids()

    seen = {"n": 0}

    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        if url.endswith("ClientLogin"):
            return 200, b"Auth=tok", {}
        seen["n"] += 1
        return (403, b"", {}) if seen["n"] == 1 else (200, b'{"itemRefs": [{"id": 1}]}', {})

    install(monkeypatch, handler)
    assert await GReaderClient("https://h", "u", "p").get_unread_item_ids() == {"1"}


async def test_set_read_state_batches_by_fifty_and_uses_add_remove(monkeypatch) -> None:
    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        return (200, b"tk", {})

    script = install(monkeypatch, handler)
    client = GReaderClient("https://h", auth_token="t")
    await client.set_read_state([], True)
    assert script.calls == []

    ids = [f"id{i}" for i in range(120)]
    await client.set_read_state(ids, False)
    posts = [kw["data"] for m, _u, kw in script.calls if m == "POST"]
    assert [sum(1 for k, _ in d if k == "i") for d in posts] == [50, 50, 20]
    assert all(("r", "user/-/state/com.google/read") in d and ("T", "tk") in d for d in posts)
    await client.set_read_state(["x"], True)
    assert ("a", "user/-/state/com.google/read") in script.calls[-1][2]["data"]


async def test_set_read_state_refreshes_token_on_403_then_fails(monkeypatch) -> None:
    tokens = iter(["old", "new"])
    posts: list[list[tuple[str, str]]] = []

    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        if method == "GET":
            return 200, next(tokens).encode(), {}
        posts.append(list(kw["data"]))
        return (403, b"", {}) if len(posts) == 1 else (200, b"OK", {})

    install(monkeypatch, handler)
    await GReaderClient("https://h", auth_token="t").set_read_state(["a"], True)
    assert posts[0][0] == ("T", "old") and posts[1][0] == ("T", "new")

    install(monkeypatch, lambda m, *_a, **_k: (200, b"t", {}) if m == "GET" else (500, b"nope", {}))
    with pytest.raises(GReaderError, match="HTTP 500: nope"):
        await GReaderClient("https://h", auth_token="t").set_read_state(["a"], True)


async def test_test_connection_truncates_token_preview(monkeypatch) -> None:
    def handler(method: str, url: str, kw: dict[str, Any]) -> Response:
        if url.endswith("ClientLogin"):
            return 200, b"Auth=abcdefghijkl", {}
        if url.endswith("/token"):
            return 200, b"t", {}
        return 200, _stream([{"id": "a"}, {"id": "b"}]), {}

    install(monkeypatch, handler)
    res = await GReaderClient("https://h", "u", "p").test_connection()
    assert res == {"ok": True, "items_accessible": 2, "auth_token_preview": "abcdefgh…"}


async def test_http_request_helper_forwards_to_httpx(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kw: Any) -> None:
            seen["init"] = kw

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
            seen["req"] = (method, url, kw)
            return httpx.Response(201, content=b"hi", headers={"X-A": "b"})

    monkeypatch.setattr(greader.httpx, "AsyncClient", FakeClient)
    status, body, headers = await greader._http_request(
        "GET", "https://h/x", headers={"X-Extra": "1"}, params={"a": "b"}, request_timeout=3
    )
    assert (status, body, headers["x-a"]) == (201, b"hi", "b")
    assert seen["init"]["timeout"] == 3
    assert seen["req"][2]["headers"] == {"User-Agent": greader.USER_AGENT, "X-Extra": "1"}


# --- diagnose -----------------------------------------------------------------


def test_candidate_bases_strip_pasted_tails_and_dedupe() -> None:
    assert diagnose.candidate_bases("https://h.example/accounts/ClientLogin/") == [
        "https://h.example",
        "https://h.example/api/greader.php",
    ]
    assert diagnose.candidate_bases("https://h.example/rss/reader/api/0") == [
        "https://h.example/rss",
        "https://h.example",
        "https://h.example/rss/api/greader.php",
        "https://h.example/api/greader.php",
    ]
    assert diagnose.candidate_bases(" not a url ") == ["not a url"]


async def test_probe_classifies_responses(monkeypatch) -> None:
    async def make(result: Any) -> None:
        async def fake(*_a: Any, **_k: Any) -> Response:
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(greader, "_http_request", fake)

    await make((200, b"Auth=zz", {}))
    assert await diagnose._probe("https://h", "u", "p") == "works"
    await make((200, b"no token", {}))
    assert await diagnose._probe("https://h", "u", "p") == "absent"
    await make((403, b"", {}))
    assert await diagnose._probe("https://h", "u", "p") == "refuses"
    await make((404, b"", {}))
    assert await diagnose._probe("https://h", "u", "p") == "absent"
    await make(httpx.ConnectError("down"))
    assert await diagnose._probe("https://h", "u", "p") == "absent"


async def test_detect_service_matches_body_or_headers(monkeypatch) -> None:
    assert await diagnose.detect_service("nonsense") is None

    async def body_hit(*_a: Any, **_k: Any) -> Response:
        return 200, b"<title>FreshRSS</title>", {}

    async def header_hit(*_a: Any, **_k: Any) -> Response:
        return 200, b"nothing", {"Server": "Miniflux"}

    async def miss(*_a: Any, **_k: Any) -> Response:
        return 200, b"nothing", {}

    async def boom(*_a: Any, **_k: Any) -> Response:
        raise OSError

    for fn, expected in [
        (body_hit, "FreshRSS"),
        (header_hit, "Miniflux"),
        (miss, None),
        (boom, None),
    ]:
        monkeypatch.setattr(greader, "_http_request", fn)
        assert await diagnose.detect_service("https://h.example/x") == expected


async def test_diagnose_login_messages(monkeypatch) -> None:
    outcomes: dict[str, str] = {}

    async def fake_probe(base: str, _u: str, _p: str) -> str:
        return outcomes.get(base, "absent")

    async def no_service(_e: str) -> str | None:
        return None

    monkeypatch.setattr(diagnose, "_probe", fake_probe)
    monkeypatch.setattr(diagnose, "detect_service", no_service)

    msg = await diagnose.diagnose_login("https://h.example/", "u", "p", "boom")
    assert msg.startswith("No Google Reader API answered") and msg.endswith("(boom)")

    outcomes["https://h.example"] = "refuses"
    msg = await diagnose.diagnose_login("https://h.example/", "u", "p", "boom")
    assert "refused the login" in msg and "API password" in msg and "at https" not in msg

    outcomes.clear()
    outcomes["https://h.example/api/greader.php"] = "refuses"
    msg = await diagnose.diagnose_login("https://h.example", "u", "p", "e")
    assert "answers at https://h.example/api/greader.php but refused" in msg

    outcomes.clear()
    outcomes["https://h.example/api/greader.php"] = "works"

    async def freshrss(_e: str) -> str:
        return "FreshRSS"

    monkeypatch.setattr(diagnose, "detect_service", freshrss)
    msg = await diagnose.diagnose_login("https://h.example", "u", "p", "e")
    assert "https://h.example/api/greader.php does. Use it." in msg
    assert msg.endswith("Detected service: FreshRSS.")


# --- reader-accounts API ------------------------------------------------------

PAYLOAD = {
    "title": " Mine ",
    "api_base_url": " https://r.example ",
    "username": "u",
    "password": "p",
}


async def test_create_bad_login_returns_diagnosis(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import reader_accounts as api

    await setup_admin(client)

    async def deny(self: GReaderClient) -> str:
        raise GReaderAuthError("Invalid credentials")

    async def hint(base: str, user: str, pw: str, err: str) -> str:
        return f"HINT[{base}|{user}|{err}]"

    monkeypatch.setattr(GReaderClient, "authenticate", deny)
    monkeypatch.setattr(api, "diagnose_login", hint)
    r = await client.post("/api/reader-accounts", json=PAYLOAD)
    assert r.status_code == 400
    assert r.json()["detail"] == "HINT[ https://r.example |u|Invalid credentials]"


async def test_create_checkpoint_failure_is_400_and_stores_nothing(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import reader_accounts as api

    await setup_admin(client)

    async def ok(self: GReaderClient) -> str:
        return "t"

    async def bad_checkpoint(_c: object, _a: object) -> None:
        raise httpx.ConnectError("")

    monkeypatch.setattr(GReaderClient, "authenticate", ok)
    monkeypatch.setattr(api, "establish_reader_checkpoint", bad_checkpoint)
    r = await client.post("/api/reader-accounts", json=PAYLOAD)
    assert r.status_code == 400 and r.json()["detail"] == "ConnectError"
    async with db_session() as s:
        assert (await s.scalars(select(ReaderAccount))).all() == []


async def _create(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from app.api import reader_accounts as api

    async def ok(self: GReaderClient) -> str:
        return "t"

    async def checkpoint(_c: object, _a: object) -> None:
        return None

    monkeypatch.setattr(GReaderClient, "authenticate", ok)
    monkeypatch.setattr(api, "establish_reader_checkpoint", checkpoint)
    r = await client.post("/api/reader-accounts", json=PAYLOAD)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def test_create_strips_fields_and_patch_can_clear_backfill(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    acc = await _create(client, monkeypatch)
    assert acc["api_base_url"] == "https://r.example" and acc["title"] == "Mine"
    aid = acc["id"]

    r = await client.patch(
        f"/api/reader-accounts/{aid}", json={"backfill_days": 5, "title": " T2 "}
    )
    assert r.json()["backfill_days"] == 5 and r.json()["title"] == "T2"
    r = await client.patch(
        f"/api/reader-accounts/{aid}", json={"backfill_days": None, "title": None}
    )
    assert r.json()["backfill_days"] is None and r.json()["title"] == "T2"
    assert (await client.patch("/api/reader-accounts/999", json={})).status_code == 404


async def test_test_endpoint_persists_new_token_and_reports_failure(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import reader_accounts as api

    await setup_admin(client)
    aid = (await _create(client, monkeypatch))["id"]

    async def good(self: GReaderClient) -> dict[str, Any]:
        self.auth_token = "rotated"
        return {"items_accessible": 4}

    monkeypatch.setattr(GReaderClient, "test_connection", good)
    r = await client.post(f"/api/reader-accounts/{aid}/test")
    assert r.json() == {"ok": True, "items_accessible": 4, "error": None}
    async with db_session() as s:
        assert (await s.get(ReaderAccount, aid)).auth_token == "rotated"

    async def hint(base: str, user: str, pw: str, err: str) -> str:
        return f"diag:{err}"

    async def net_fail(self: GReaderClient) -> dict[str, Any]:
        raise GReaderError("")

    async def other_fail(self: GReaderClient) -> dict[str, Any]:
        raise ValueError("weird")

    monkeypatch.setattr(api, "diagnose_login", hint)
    monkeypatch.setattr(GReaderClient, "test_connection", net_fail)
    r = await client.post(f"/api/reader-accounts/{aid}/test")
    assert r.json()["ok"] is False and r.json()["error"] == "diag:GReaderError"
    monkeypatch.setattr(GReaderClient, "test_connection", other_fail)
    r = await client.post(f"/api/reader-accounts/{aid}/test")
    assert r.json() == {"ok": False, "items_accessible": 0, "error": "weird"}


async def test_poll_endpoint_success_disabled_and_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import reader_accounts as api

    await setup_admin(client)
    aid = (await _create(client, monkeypatch))["id"]

    async def good(_s: object, _a: object) -> dict[str, int]:
        return {"new_articles": 3, "read_synced": 1}

    async def bad(_s: object, _a: object) -> dict[str, int]:
        raise RuntimeError("upstream")

    monkeypatch.setattr(api, "poll_reader_account", good)
    r = await client.post(f"/api/reader-accounts/{aid}/poll")
    assert r.json() == {"new_articles": 3, "read_synced": 1}

    monkeypatch.setattr(api, "poll_reader_account", bad)
    r = await client.post(f"/api/reader-accounts/{aid}/poll")
    assert r.status_code == 502 and "upstream" in r.json()["detail"]

    await client.patch(f"/api/reader-accounts/{aid}", json={"is_enabled": False})
    assert (await client.post(f"/api/reader-accounts/{aid}/poll")).status_code == 400
    await client.delete(f"/api/reader-accounts/{aid}")
    assert (await client.post(f"/api/reader-accounts/{aid}/poll")).status_code == 404
