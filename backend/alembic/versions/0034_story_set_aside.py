"""Stories kept hidden after their mute rule ended: story_set_aside

Revision ID: 0034_story_set_aside
Revises: 0033_topic_groups
Create Date: 2026-11-22 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_story_set_aside"
down_revision: str | None = "0033_topic_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "story_set_aside",
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column(
            "story_id",
            sa.Integer(),
            sa.ForeignKey("story.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("set_aside_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_story_set_aside_story_id", "story_set_aside", ["story_id"])


def downgrade() -> None:
    op.drop_index("ix_story_set_aside_story_id", table_name="story_set_aside")
    op.drop_table("story_set_aside")
