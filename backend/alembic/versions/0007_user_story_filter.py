"""per-user story filter pref on user

Revision ID: 0007_user_story_filter
Revises: 0006_user_story_prefs
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_user_story_filter"
down_revision: str | None = "0006_user_story_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty string = follow the server default (unread)
    op.add_column(
        "user",
        sa.Column("story_filter", sa.String(16), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user", "story_filter")
