"""category_suggestion + category_proposal_dismissal: recurring-new-category detection

Revision ID: 0014_category_suggestions
Revises: 0013_feed_email_count
Create Date: 2026-09-14

The summarize LLM call may propose a new category when none of the current
taxonomy fits well; suggestions are logged (never auto-applied) so an admin
can review recurring proposals on the Settings page and decide whether to
add them to the taxonomy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_category_suggestions"
down_revision: str | None = "0013_feed_email_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "category_suggestion",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.String(length=128), nullable=False),
        sa.Column("normalized_text", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_category_suggestion_normalized_text",
        "category_suggestion",
        ["normalized_text"],
    )
    op.create_table(
        "category_proposal_dismissal",
        sa.Column("normalized_text", sa.String(length=128), primary_key=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("category_proposal_dismissal")
    op.drop_index(
        "ix_category_suggestion_normalized_text", table_name="category_suggestion"
    )
    op.drop_table("category_suggestion")
