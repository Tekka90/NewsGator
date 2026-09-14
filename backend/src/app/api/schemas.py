"""Pydantic schemas for the API."""

from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl

# --- auth / users ---


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    """First-run: creates the admin account. Rejected once any user exists."""

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8)


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    summary_language: str
    # "" = follow the server default (published, oldest first)
    story_sort: str = ""
    story_order: str = ""
    # "" = follow the server default (unread)
    story_filter: str = ""

    model_config = {"from_attributes": True}


class AuthOut(UserOut):
    """Login/setup response: user plus a portable session token.

    iOS standalone (home-screen) PWAs do not reliably persist cookies across
    app restarts, so clients may keep this token and send it as
    `Authorization: Bearer <token>` instead of relying on the cookie.
    """

    token: str


class MePatch(BaseModel):
    summary_language: str | None = None
    password: str | None = Field(default=None, min_length=8)
    story_sort: str | None = Field(default=None, pattern="^(updated|published|sources)$")
    story_order: str | None = Field(default=None, pattern="^(asc|desc)$")
    story_filter: str | None = Field(default=None, pattern="^(all|unread|updated)$")


# --- admin user management ---


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8)
    is_admin: bool = False


class UserPatchIn(BaseModel):
    password: str | None = Field(default=None, min_length=8)
    is_admin: bool | None = None


class ManagedUserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    summary_language: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- feeds ---


class FeedIn(BaseModel):
    url: HttpUrl
    title: str = ""
    poll_interval_min: int = Field(default=30, ge=5, le=1440)
    auth_cookies: str | None = None
    fetch_fulltext: bool = True
    # First-poll backfill window; omit to use the server default, 0 = everything
    backfill_days: int | None = Field(default=None, ge=0, le=3650)


class FeedPatch(BaseModel):
    title: str | None = None
    poll_interval_min: int | None = Field(default=None, ge=5, le=1440)
    is_enabled: bool | None = None
    auth_cookies: str | None = None
    fetch_fulltext: bool | None = None
    backfill_days: int | None = Field(default=None, ge=0, le=3650)


class FeedOut(BaseModel):
    id: int
    url: str
    # 'rss' or 'mail' (newsletter ingestion; never RSS-polled)
    kind: str = "rss"
    sender_email: str | None = None
    title: str
    is_enabled: bool
    poll_interval_min: int
    backfill_days: int | None = None
    last_fetched_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    fetch_fulltext: bool
    # populated by GET /feeds only (per requesting user for unread) — 0 elsewhere
    story_count: int = 0
    unread_story_count: int = 0
    # Mail feeds only: newsletter emails processed for this sender (0 for RSS)
    email_count: int = 0

    model_config = {"from_attributes": True}


# --- newsletter ingestion (per-user IMAP accounts) ---


class MailAccountIn(BaseModel):
    host: str = Field(min_length=1, max_length=256)
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=512)
    folder: str = Field(min_length=1, max_length=256)  # mandatory (SPEC §9)
    use_ssl: bool = True


class MailAccountPatch(BaseModel):
    host: str | None = Field(default=None, min_length=1, max_length=256)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=256)
    # None = unchanged; the password can be replaced but never cleared or read back
    password: str | None = Field(default=None, min_length=1, max_length=512)
    folder: str | None = Field(default=None, min_length=1, max_length=256)
    use_ssl: bool | None = None
    is_enabled: bool | None = None


class MailAccountOut(BaseModel):
    id: int
    host: str
    port: int
    username: str
    folder: str
    use_ssl: bool
    is_enabled: bool
    last_uid: int
    last_checked_at: datetime | None
    last_error: str | None
    created_at: datetime
    # password is deliberately never returned

    model_config = {"from_attributes": True}


# --- categories ---


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class CategoryOut(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class CategorySuggestionOut(BaseModel):
    normalized_text: str
    label: str
    article_count: int
    first_seen: datetime
    last_seen: datetime
    example_titles: list[str]


class CategorySuggestionAction(BaseModel):
    normalized_text: str
    label: str = ""  # only needed for /accept (creates a Category named `label`)


# --- misc ---


class HealthOut(BaseModel):
    status: str
    version: str
