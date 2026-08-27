"""llm usage metrics table

Revision ID: 0010_llm_usage
Revises: 0009_story_readeck
Create Date: 2026-08-27

One row per external LLM call (chat or embedding). Append-only, never purged by
retention; article/story/feed ids are plain ints (no FKs) so the history
survives article retention and feed deletion.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_llm_usage"
down_revision: str | None = "0009_story_readeck"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(8), nullable=False, server_default="chat"),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("article_id", sa.Integer(), nullable=True),
        sa.Column("story_id", sa.Integer(), nullable=True),
        sa.Column("feed_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_llm_usage_ts", "llm_usage", ["ts"])
    op.create_index("ix_llm_usage_kind", "llm_usage", ["kind"])
    op.create_index("ix_llm_usage_article_id", "llm_usage", ["article_id"])
    op.create_index("ix_llm_usage_feed_id", "llm_usage", ["feed_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_feed_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_article_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_kind", table_name="llm_usage")
    op.drop_index("ix_llm_usage_ts", table_name="llm_usage")
    op.drop_table("llm_usage")
