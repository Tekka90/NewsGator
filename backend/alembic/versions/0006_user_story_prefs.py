"""per-user story sort/order prefs on user

Revision ID: 0006_user_story_prefs
Revises: 0005_image_urls
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_user_story_prefs"
down_revision: str | None = "0005_image_urls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty string = follow the server default (published, oldest first)
    op.add_column(
        "user",
        sa.Column("story_sort", sa.String(16), server_default="", nullable=False),
    )
    op.add_column(
        "user",
        sa.Column("story_order", sa.String(8), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user", "story_order")
    op.drop_column("user", "story_sort")
