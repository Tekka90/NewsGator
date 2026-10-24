"""per-user Listen resume position (listen_position)

Revision ID: 0029_listen_position
Revises: 0028_article_translation
Create Date: 2026-10-24 08:30:00.000000

One row per (user, story): where the user stopped listening, synced across devices.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_listen_position"
down_revision: str | None = "0028_article_translation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "listen_position",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id"), primary_key=True),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=True),
        sa.Column("chunk", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("listen_position")
