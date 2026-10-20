"""Covering index (story_id, published_at, fetched_at) on article.

Story list ordering and source counts aggregate article dates per story; this index
answers them without reading the large article text columns.

Revision ID: 0023_article_story_dates_index
Revises: 0022_reader_initial_import
Create Date: 2026-10-20 23:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_article_story_dates_index"
down_revision: str | None = "0022_reader_initial_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_article_story_dates",
        "article",
        ["story_id", "published_at", "fetched_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_article_story_dates", table_name="article", if_exists=True)
