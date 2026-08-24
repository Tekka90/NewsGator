"""feed.empty_polls for adaptive polling

Revision ID: 0002_feed_empty_polls
Revises: 0001_initial
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_feed_empty_polls"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feed",
        sa.Column("empty_polls", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("feed", "empty_polls")
