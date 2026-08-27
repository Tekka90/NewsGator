"""readeck bookmark id on story

Revision ID: 0009_story_readeck
Revises: 0008_feed_backfill_days
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_story_readeck"
down_revision: str | None = "0008_feed_backfill_days"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Set when the story was successfully pushed to Readeck (permanent archive).
    op.add_column("story", sa.Column("readeck_bookmark_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("story", "readeck_bookmark_id")
