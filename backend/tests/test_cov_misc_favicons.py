# ruff: noqa: E501
"""Favicon discovery against a mocked HTTP layer: conventional path, <link rel=icon>, parent hosts."""

from collections.abc import Callable

import httpx
import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.api import favicons

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
Handler = Callable[[httpx.Request], httpx.Response]
REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _clear() -> None:
    favicons._cache.clear()


def mock_http(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[str]:
    requested: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    def factory(**kw: object) -> httpx.AsyncClient:
        return REAL_CLIENT(transport=httpx.MockTransport(wrapped), **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(favicons.httpx, "AsyncClient", factory)
    return requested


def test_icon_links_only_icon_rels_in_order() -> None:
    html = (
        '<link rel="stylesheet" href="/a.css">'
        "<link href='/first.png' rel='shortcut icon'>"
        '<link rel="apple-touch-icon" href="/apple.png">'
        '<link rel="icon">'
        '<link rel="icon" href="/last.svg">'
    )
    assert favicons._icon_links(html) == ["/first.png", "/apple.png", "/last.svg"]


async def test_conventional_path_wins_and_media_type_is_cleaned(monkeypatch) -> None:
    requested = mock_http(
        monkeypatch,
        lambda r: httpx.Response(
            200, content=PNG, headers={"content-type": "image/png; charset=x"}
        ),
    )
    content, media = await favicons._fetch_host_favicon("news.example.com")
    assert (content, media) == (PNG, "image/png")
    assert requested == ["https://news.example.com/favicon.ico"]


async def test_falls_back_to_homepage_link_skipping_bad_and_insecure_icons(monkeypatch) -> None:
    page = (
        '<link rel="icon" href="http://plain.example/insecure.png">'
        '<link rel="icon" href="/missing.png">'
        '<link rel="icon" href="/huge.png">'
        '<link rel="icon" href="/good.png">'
    )

    def handler(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path == "/favicon.ico" or path == "/missing.png":
            return httpx.Response(404)
        if path == "/":
            return httpx.Response(200, text=page)
        if path == "/huge.png":
            return httpx.Response(200, content=b"x" * (favicons._MAX_BYTES + 1))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    requested = mock_http(monkeypatch, handler)
    assert await favicons._fetch_host_favicon("site.example") == (PNG, "image/png")
    assert not any("insecure" in u for u in requested)  # http:// icons are never fetched


async def test_empty_favicon_and_no_links_raises(monkeypatch) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/favicon.ico":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, text="<html>no icons</html>")

    mock_http(monkeypatch, handler)
    with pytest.raises(ValueError, match="no favicon"):
        await favicons._fetch_host_favicon("site.example")


async def test_walks_up_to_parent_domain_but_stops_at_two_labels(monkeypatch) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "example.com" and r.url.path == "/favicon.ico":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)

    requested = mock_http(monkeypatch, handler)
    assert await favicons._fetch_favicon("mail.news.example.com") == (PNG, "image/png")
    hosts = [u.split("/")[2] for u in requested if u.endswith("favicon.ico")]
    assert hosts == ["mail.news.example.com", "news.example.com", "example.com"]

    mock_http(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await favicons._fetch_favicon("a.b")


async def test_endpoint_validates_host_lowercases_and_sets_fallback_media(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    seen: list[str] = []

    async def fake(host: str) -> tuple[bytes, str]:
        seen.append(host)
        return PNG, "application/octet-stream"

    monkeypatch.setattr(favicons, "_fetch_favicon", fake)
    for bad in ["bad host", "a..b", "-lead.com", "x/y", "a_b.com"]:
        assert (await client.get("/api/favicon", params={"host": bad})).status_code == 400, bad
    r = await client.get("/api/favicon", params={"host": "News.Example.COM"})
    assert seen == ["news.example.com"]
    assert r.headers["content-type"] == "image/x-icon"  # non-image media is normalised
    assert (await client.get("/api/favicon")).status_code == 422
    client.cookies.clear()
    assert (await client.get("/api/favicon", params={"host": "a.com"})).status_code == 401


async def test_expired_cache_entry_is_refetched(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_admin(client)
    favicons._cache["old.example.com"] = (0.0, None, "")  # expired failure
    calls = 0

    async def fake(host: str) -> tuple[bytes, str]:
        nonlocal calls
        calls += 1
        return PNG, "image/png"

    monkeypatch.setattr(favicons, "_fetch_favicon", fake)
    assert (await client.get("/api/favicon", params={"host": "old.example.com"})).status_code == 200
    assert calls == 1
