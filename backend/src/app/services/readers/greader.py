"""Google Reader API client (SPEC §9 third-party reader ingestion).

Supports Google Reader API (/reader/api/0) compatible RSS readers:
Inoreader, FreshRSS, Miniflux, The Old Reader, BazQux, etc.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.services.ingest import canonicalize_url

USER_AGENT = "NewsGator/0.1 (+self-hosted feed reader)"


class GReaderError(Exception):
    """Base exception for Google Reader API failures."""


class GReaderAuthError(GReaderError):
    """Authentication or authorization failure."""


@dataclass
class GReaderItem:
    id: str
    title: str
    url: str
    raw_content: str
    published_at: datetime | None
    origin_feed_title: str | None
    is_read: bool


async def _http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | list[tuple[str, str]] | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes, dict[str, str]]:
    """Low-level HTTP request helper (module-level seam for tests)."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        resp = await client.request(
            method,
            url,
            headers=req_headers,
            params=params,
            data=data,
        )
        return resp.status_code, resp.content, dict(resp.headers)


class GReaderClient:
    """Async client communicating via the Google Reader API standard."""

    def __init__(
        self,
        api_base_url: str,
        username: str = "",
        password: str = "",
        auth_token: str | None = None,
    ) -> None:
        self.base_url = api_base_url.rstrip("/")
        self.username = username
        self.password = password
        self.auth_token = auth_token
        self._action_token: str | None = None

    def _resolve_url(self, relative_path: str) -> str:
        """Resolve a relative endpoint path against base_url."""
        path = relative_path.lstrip("/")
        return f"{self.base_url}/{path}"

    def _auth_headers(self) -> dict[str, str]:
        if not self.auth_token:
            return {}
        token = self.auth_token
        if not token.startswith("GoogleLogin auth=") and not token.startswith("Bearer "):
            token = f"GoogleLogin auth={token}"
        return {"Authorization": token}

    async def authenticate(self) -> str:
        """Obtain or verify an Auth token using ClientLogin or provided credentials."""
        # If an auth token is already explicitly provided, probe its validity
        if self.auth_token:
            try:
                await self.get_token()
                return self.auth_token
            except GReaderAuthError:
                # Token expired or invalid; proceed to login if username & password exist
                if not (self.username and self.password):
                    raise
                self.auth_token = None

        if not self.username or not self.password:
            raise GReaderAuthError("Username and password are required for ClientLogin")

        login_url = self._resolve_url("accounts/ClientLogin")
        data = {
            "Email": self.username,
            "Passwd": self.password,
            "accountType": "HOSTED_OR_GOOGLE",
            "service": "reader",
        }
        status_code, content, _ = await _http_request("POST", login_url, data=data)
        if status_code == 404:
            # Fall back to root origin /accounts/ClientLogin if base_url had subpath
            parsed = urlparse(self.base_url)
            root_login = f"{parsed.scheme}://{parsed.netloc}/accounts/ClientLogin"
            status_code, content, _ = await _http_request("POST", root_login, data=data)

        if status_code in (401, 403):
            raise GReaderAuthError("Invalid credentials")
        if status_code >= 400:
            raise GReaderError(f"ClientLogin failed with HTTP {status_code}: {content.decode(errors='replace')}")

        text = content.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("Auth="):
                token = line[5:].strip()
                self.auth_token = token
                return token

        raise GReaderAuthError("ClientLogin response did not contain an Auth token")

    async def get_token(self) -> str:
        """Fetch action token (CSRF token) needed for state mutations (edit-tag)."""
        token_url = self._resolve_url("reader/api/0/token")
        status_code, content, _ = await _http_request(
            "GET", token_url, headers=self._auth_headers()
        )
        if status_code in (401, 403):
            # Attempt re-authentication once
            if self.username and self.password:
                await self.authenticate()
                status_code, content, _ = await _http_request(
                    "GET", token_url, headers=self._auth_headers()
                )
        if status_code in (401, 403):
            raise GReaderAuthError("Unauthorized token request")
        if status_code >= 400:
            raise GReaderError(f"Token request failed with HTTP {status_code}")

        token = content.decode("utf-8", errors="replace").strip()
        self._action_token = token
        return token

    async def fetch_stream(
        self,
        continuation: str | None = None,
        limit: int = 50,
        exclude_read: bool = False,
    ) -> tuple[list[GReaderItem], str | None]:
        """Fetch reading list entries. Returns (items, new_continuation_token)."""
        if not self.auth_token and (self.username and self.password):
            await self.authenticate()

        stream_url = self._resolve_url("reader/api/0/stream/contents/user/-/state/com.google/reading-list")
        params: dict[str, Any] = {
            "output": "json",
            "n": str(limit),
        }
        if continuation:
            params["c"] = continuation
        if exclude_read:
            params["xt"] = "user/-/state/com.google/read"

        status_code, content, _ = await _http_request(
            "GET", stream_url, headers=self._auth_headers(), params=params
        )
        if status_code in (401, 403):
            if self.username and self.password:
                await self.authenticate()
                status_code, content, _ = await _http_request(
                    "GET", stream_url, headers=self._auth_headers(), params=params
                )
        if status_code in (401, 403):
            raise GReaderAuthError("Unauthorized reading list fetch")
        if status_code >= 400:
            raise GReaderError(f"Fetch stream failed with HTTP {status_code}")

        import json

        data = json.loads(content)
        new_continuation = data.get("continuation")
        raw_items = data.get("items", [])
        items: list[GReaderItem] = []

        for raw in raw_items:
            item_id = str(raw.get("id", ""))
            if not item_id:
                continue

            # Link handling: prioritize canonical href, then alternate href
            link = ""
            for c in raw.get("canonical", []):
                if isinstance(c, dict) and c.get("href"):
                    link = str(c["href"])
                    break
            if not link:
                for a in raw.get("alternate", []):
                    if isinstance(a, dict) and a.get("href"):
                        link = str(a["href"])
                        break
            if not link:
                link = item_id

            # Canonicalize publisher URL to strip tracking params
            publisher_url = canonicalize_url(link)

            # Date handling
            published_at: datetime | None = None
            raw_pub = raw.get("published")
            if raw_pub is not None:
                try:
                    published_at = datetime.fromtimestamp(float(raw_pub), tz=UTC)
                except (ValueError, TypeError, OverflowError):
                    pass
            if published_at is None and raw.get("crawlTimeMsec"):
                try:
                    published_at = datetime.fromtimestamp(float(raw["crawlTimeMsec"]) / 1000.0, tz=UTC)
                except (ValueError, TypeError, OverflowError):
                    pass

            # Origin title (sub-feed display name, e.g. "Ars Technica")
            origin = raw.get("origin", {})
            origin_title = origin.get("title") if isinstance(origin, dict) else None

            # Read status (exact state tag; avoid prefix-matching 'reading-list')
            categories = raw.get("categories", [])
            is_read = any(cat.endswith("/state/com.google/read") for cat in categories if isinstance(cat, str))

            # Content
            content_val = ""
            if isinstance(raw.get("content"), dict) and raw["content"].get("content"):
                content_val = str(raw["content"]["content"])
            elif isinstance(raw.get("summary"), dict) and raw["summary"].get("content"):
                content_val = str(raw["summary"]["content"])

            items.append(
                GReaderItem(
                    id=item_id,
                    title=str(raw.get("title", "")),
                    url=publisher_url,
                    raw_content=content_val,
                    published_at=published_at,
                    origin_feed_title=origin_title,
                    is_read=is_read,
                )
            )

        return items, new_continuation

    async def get_unread_item_ids(self) -> set[str]:
        """Fetch list of all currently unread item IDs."""
        if not self.auth_token and (self.username and self.password):
            await self.authenticate()

        ids_url = self._resolve_url("reader/api/0/stream/items/ids")
        params = {
            "output": "json",
            "s": "user/-/state/com.google/reading-list",
            "xt": "user/-/state/com.google/read",
        }
        status_code, content, _ = await _http_request(
            "GET", ids_url, headers=self._auth_headers(), params=params
        )
        if status_code in (401, 403):
            if self.username and self.password:
                await self.authenticate()
                status_code, content, _ = await _http_request(
                    "GET", ids_url, headers=self._auth_headers(), params=params
                )
        if status_code in (401, 403):
            raise GReaderAuthError("Unauthorized item IDs request")
        if status_code >= 400:
            raise GReaderError(f"Get item IDs failed with HTTP {status_code}")

        import json

        data = json.loads(content)
        item_refs = data.get("itemRefs", [])
        return {str(ref["id"]) for ref in item_refs if isinstance(ref, dict) and "id" in ref}

    async def set_read_state(self, item_ids: list[str], is_read: bool) -> None:
        """Push read/unread state upstream for a list of item IDs via edit-tag."""
        if not item_ids:
            return

        token = await self.get_token()
        edit_url = self._resolve_url("reader/api/0/edit-tag")

        tag = "user/-/state/com.google/read"
        # Support batching up to 50 items per call
        batch_size = 50
        for i in range(0, len(item_ids), batch_size):
            batch = item_ids[i : i + batch_size]
            data: list[tuple[str, str]] = [("T", token)]
            if is_read:
                data.append(("a", tag))
            else:
                data.append(("r", tag))
            for item_id in batch:
                data.append(("i", item_id))

            status_code, content, _ = await _http_request(
                "POST", edit_url, headers=self._auth_headers(), data=data
            )
            if status_code in (401, 403):
                # Token may have expired, retry once
                token = await self.get_token()
                data[0] = ("T", token)
                status_code, content, _ = await _http_request(
                    "POST", edit_url, headers=self._auth_headers(), data=data
                )
            if status_code >= 400:
                raise GReaderError(
                    f"edit-tag failed with HTTP {status_code}: {content.decode('utf-8', errors='replace')}"
                )

    async def test_connection(self) -> dict[str, Any]:
        """Probe authentication and stream access."""
        auth_token = await self.authenticate()
        token = await self.get_token()
        items, _ = await self.fetch_stream(limit=3)
        return {
            "ok": True,
            "items_accessible": len(items),
            "auth_token_preview": auth_token[:8] + "…" if len(auth_token) > 8 else auth_token,
        }
