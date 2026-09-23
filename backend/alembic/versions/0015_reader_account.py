"""third-party RSS reader API accounts (reader_account) + article origin_feed_title

Revision ID: 0015_reader_account
Revises: 0014_category_suggestions
Create Date: 2026-09-23

Per-user third-party RSS reader API accounts (Google Reader API compatible);
all articles pulled from an account are grouped into one single virtual Feed
(feed.kind = 'reader_api'); article.origin_feed_title captures the original
remote subscription title for accurate attribution in story views.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_reader_account"
down_revision: str | None = "0014_category_suggestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reader_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="greader"),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("api_base_url", sa.String(1024), nullable=False),
        sa.Column("username", sa.String(256), nullable=False, server_default=""),
        sa.Column("password", sa.String(512), nullable=False, server_default=""),
        sa.Column("auth_token", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("virtual_feed_id", sa.Integer(), sa.ForeignKey("feed.id"), nullable=True),
        sa.Column("sync_cursor", sa.String(256), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reader_account_user_id", "reader_account", ["user_id"])
    op.add_column("article", sa.Column("origin_feed_title", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("article", "origin_feed_title")
    op.drop_index("ix_reader_account_user_id", table_name="reader_account")
    op.drop_table("reader_account")
