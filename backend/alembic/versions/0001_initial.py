"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("summary_language", sa.String(8), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "feed",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.String(1024), nullable=False, unique=True),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("poll_interval_min", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("etag", sa.String(512), nullable=True),
        sa.Column("last_modified", sa.String(512), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_cookies", sa.Text(), nullable=True),
        sa.Column("fetch_fulltext", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "story",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(128), nullable=False, server_default="Uncategorized"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_frozen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "article",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feed_id", sa.Integer(), sa.ForeignKey("feed.id"), nullable=False, index=True),
        sa.Column("guid", sa.String(1024), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False, server_default=""),
        sa.Column("raw_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("language", sa.String(8), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id"), nullable=True),
        sa.Column(
            "processing_state", sa.String(32), nullable=False, server_default="fetched", index=True
        ),
        sa.Column("content_status", sa.String(16), nullable=False, server_default="full"),
        sa.Column("content_warning", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "story_state",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id"), primary_key=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_at_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "story_revision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "story_id", sa.Integer(), sa.ForeignKey("story.id"), nullable=False, index=True
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "activity_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("level", sa.String(8), nullable=False, server_default="info"),
        sa.Column("component", sa.String(32), nullable=False, index=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    for table in (
        "activity_log",
        "story_revision",
        "story_state",
        "article",
        "story",
        "setting",
        "category",
        "feed",
        "user",
    ):
        op.drop_table(table)
