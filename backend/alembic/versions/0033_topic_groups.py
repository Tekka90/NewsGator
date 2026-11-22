"""Topic groups (galaxies): topic_group, topic.group_id, topic_rule.source_group_id

Revision ID: 0033_topic_groups
Revises: 0032_topic_radar
Create Date: 2026-11-22 08:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_topic_groups"
down_revision: str | None = "0032_topic_radar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_group",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(256), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("label_language", sa.String(8), nullable=False, server_default=""),
        sa.Column("centroid", sa.LargeBinary(), nullable=False),
        sa.Column("topic_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("story_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("topic") as batch:
        batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        batch.create_index("ix_topic_group_id", ["group_id"])
    with op.batch_alter_table("topic_rule") as batch:
        batch.add_column(sa.Column("source_group_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("topic_rule") as batch:
        batch.drop_column("source_group_id")
    with op.batch_alter_table("topic") as batch:
        batch.drop_index("ix_topic_group_id")
        batch.drop_column("group_id")
    op.drop_table("topic_group")
