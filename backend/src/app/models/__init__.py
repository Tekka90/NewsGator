"""SQLAlchemy models. Schema normative reference: SPEC.md §3."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """DateTime that always returns tz-aware UTC datetimes.

    SQLite stores datetimes without tzinfo, so plain DateTime(timezone=True)
    columns come back naive and crash when compared to datetime.now(UTC).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-user override; empty string = follow global SUMMARY_LANGUAGE
    summary_language: Mapped[str] = mapped_column(String(8), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Feed(Base):
    __tablename__ = "feed"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_min: Mapped[int] = mapped_column(Integer, default=30)
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    first_failure_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    # Adaptive polling: consecutive polls that produced zero new articles (SPEC §9)
    empty_polls: Mapped[int] = mapped_column(Integer, default=0)
    # Optional per-feed credentials for the user's own subscriptions (SPEC §9)
    auth_cookies: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_fulltext: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    articles: Mapped[list["Article"]] = relationship(back_populates="feed")


class Category(Base):
    """Customizable taxonomy (SPEC §8) — admins can add/rename/remove in the GUI."""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# Seeded at first migration; fully editable afterwards.
SEED_CATEGORIES = [
    "Tech",
    "World",
    "Science",
    "Business",
    "Sports",
    "Culture",
    "Politics",
    "Health",
    "Uncategorized",
]


class Setting(Base):
    """Runtime-overridable settings (admin GUI). Key/value; values JSON-encoded."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# --- Milestone 2+ tables (declared now so Alembic owns the full schema) ---


class Story(Base):
    __tablename__ = "story"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(128), default="Uncategorized")
    # Lead image: first member article that carried an RSS image (SPEC §3)
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    articles: Mapped[list["Article"]] = relationship(back_populates="story")


class Article(Base):
    __tablename__ = "article"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feed.id"), index=True)
    guid: Mapped[str] = mapped_column(String(1024))  # dedupe: (feed_id, guid)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(1024), default="")
    # Image from the RSS entry (media:content / media:thumbnail / image enclosure)
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    raw_content: Mapped[str] = mapped_column(Text, default="")
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    story_id: Mapped[int | None] = mapped_column(ForeignKey("story.id"), nullable=True)
    # fetched → fulltext → summarized → embedded → clustered (SPEC §8)
    processing_state: Mapped[str] = mapped_column(String(32), default="fetched", index=True)
    content_status: Mapped[str] = mapped_column(String(16), default="full")
    content_warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    feed: Mapped[Feed] = relationship(back_populates="articles")
    story: Mapped[Story | None] = relationship(back_populates="articles")


class StoryState(Base):
    """Per-user read state (SPEC §3, invariant 4)."""

    __tablename__ = "story_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("story.id"), primary_key=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    read_at_version: Mapped[int] = mapped_column(Integer, default=0)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class StoryRevision(Base):
    __tablename__ = "story_revision"

    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("story.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ActivityEvent(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    component: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default="{}")  # JSON


class ClusterDecision(Base):
    """Every clustering decision, for the threshold-tuning report (SPEC §5)."""

    __tablename__ = "cluster_decision"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("article.id"), index=True)
    story_id: Mapped[int | None] = mapped_column(ForeignKey("story.id"), nullable=True)
    similarity: Mapped[float | None] = mapped_column(nullable=True)
    decision: Mapped[str] = mapped_column(String(24))  # new|attach|attach_confirmed
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OverridePair(Base):
    """Manual merge/split/move corrections as labeled pairs (SPEC invariant 9).

    label: 'same' (user merged) or 'different' (user split/moved out).
    """

    __tablename__ = "override_pair"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("article.id"), index=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("story.id"))
    label: Mapped[str] = mapped_column(String(16))  # same|different
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
