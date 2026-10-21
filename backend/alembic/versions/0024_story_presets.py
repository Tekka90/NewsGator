"""story presets ("Smart Views") per user

Adds the JSON preset document (NULL = built-in defaults), the last selected preset id and
the compact-bar flag on the user table. The legacy story_sort/story_order/story_filter
columns stay for older clients.

Revision ID: 0024_story_presets
Revises: 0023_article_story_dates_index
Create Date: 2026-10-21 19:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_story_presets"
down_revision: str | None = "0023_article_story_dates_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user", sa.Column("story_presets", sa.Text(), nullable=True))
    op.add_column("user", sa.Column("last_preset_id", sa.String(length=64), nullable=True))
    op.add_column(
        "user",
        sa.Column("presets_compact", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("user", "presets_compact")
    op.drop_column("user", "last_preset_id")
    op.drop_column("user", "story_presets")
