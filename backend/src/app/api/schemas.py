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


# --- feeds ---


class FeedIn(BaseModel):
    url: HttpUrl
    title: str = ""
    poll_interval_min: int = Field(default=30, ge=5, le=1440)
    auth_cookies: str | None = None
    fetch_fulltext: bool = True


class FeedPatch(BaseModel):
    title: str | None = None
    poll_interval_min: int | None = Field(default=None, ge=5, le=1440)
    is_enabled: bool | None = None
    auth_cookies: str | None = None
    fetch_fulltext: bool | None = None


class FeedOut(BaseModel):
    id: int
    url: str
    title: str
    is_enabled: bool
    poll_interval_min: int
    last_fetched_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    fetch_fulltext: bool

    model_config = {"from_attributes": True}


# --- categories ---


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class CategoryOut(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


# --- misc ---


class HealthOut(BaseModel):
    status: str
    version: str
