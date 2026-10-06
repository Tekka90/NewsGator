"""Per-account import settings on reader_account.

Revision ID: 0021_reader_account_import
Revises: 0020_article_story_index
Create Date: 2026-10-05 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_reader_account_import"
down_revision: str | None = "0020_article_story_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("reader_account")}
    # databases created from the models (legacy create_all) may already carry these columns
    with op.batch_alter_table("reader_account") as batch:
        if "articles_per_sync" not in columns:
            batch.add_column(
                sa.Column("articles_per_sync", sa.Integer(), nullable=False, server_default="50")
            )
        if "backfill_days" not in columns:
            # NULL follows FEED_BACKFILL_DAYS; 0 imports everything
            batch.add_column(sa.Column("backfill_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reader_account") as batch:
        batch.drop_column("backfill_days")
        batch.drop_column("articles_per_sync")
