"""listen position validity stamps (story_version, text_length)

Revision ID: 0030_listen_position_validity
Revises: 0029_listen_position
Create Date: 2026-10-24 09:10:00.000000

A stored position is only valid for the story version / article text it was recorded
against; GET drops it lazily when either changed. Existing rows are stale-by-definition
(no stamp), so they are deleted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_listen_position_validity"
down_revision: str | None = "0029_listen_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM listen_position")
    with op.batch_alter_table("listen_position") as batch:
        batch.add_column(
            sa.Column("story_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("text_length", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listen_position") as batch:
        batch.drop_column("text_length")
        batch.drop_column("story_version")
