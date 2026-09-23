"""story translations cache (story_translation)

Revision ID: 0017_story_translation
Revises: 0016_user_feed
Create Date: 2026-09-23

Stores cached translations of story title and summary for users whose
configured summary_language differs from the global server default.
Clustering and embeddings remain in the server's global summary_language.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_story_translation"
down_revision: str | None = "0016_user_feed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "story_translation",
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id"), primary_key=True),
        sa.Column("language", sa.String(length=8), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("story_translation")
