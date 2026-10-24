"""article translation cache (article_translation)

Revision ID: 0028_article_translation
Revises: 0027_presets_video_kind
Create Date: 2026-10-23 22:30:00.000000

Caches the translated cleaned full text of an article (Listen player), per target language.
`source_hash` fingerprints the cleaned source text so a changed full_text invalidates the row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_article_translation"
down_revision: str | None = "0027_presets_video_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_translation",
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("article.id"), primary_key=True),
        sa.Column("language", sa.String(length=8), primary_key=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("article_translation")
