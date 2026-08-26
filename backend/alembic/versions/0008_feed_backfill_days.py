"""per-feed backfill window on feed

Revision ID: 0008_feed_backfill_days
Revises: 0007_user_story_filter
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_feed_backfill_days"
down_revision: str | None = "0007_user_story_filter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL = follow the server default (FEED_BACKFILL_DAYS); 0 = import everything
    op.add_column("feed", sa.Column("backfill_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("feed", "backfill_days")
