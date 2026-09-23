"""Add language column to story.

Revision ID: 0018_story_language
Revises: 0017_story_translation
Create Date: 2026-09-23 20:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0018_story_language'
down_revision: str | None = '0017_story_translation'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'story',
        sa.Column('language', sa.String(length=8), server_default=sa.text("''"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column('story', 'language')

