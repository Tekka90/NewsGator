"""cluster decisions + manual override pairs

Revision ID: 0004_cluster_tables
Revises: 0003_vec_tables
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_cluster_tables"
down_revision: str | None = "0003_vec_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cluster_decision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id", sa.Integer(), sa.ForeignKey("article.id"), nullable=False,
            index=True,
        ),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id"), nullable=True),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "override_pair",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "article_id", sa.Integer(), sa.ForeignKey("article.id"), nullable=False,
            index=True,
        ),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id"), nullable=False),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("override_pair")
    op.drop_table("cluster_decision")
