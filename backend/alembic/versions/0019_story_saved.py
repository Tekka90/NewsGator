"""Add saved_at to story_state.

Revision ID: 0019_story_saved
Revises: 0018_story_language
Create Date: 2026-09-30 17:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019_story_saved'
down_revision: str | None = '0018_story_language'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('story_state', sa.Column('saved_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('story_state', 'saved_at')
