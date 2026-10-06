"""Reader accounts: initial import count and incremental checkpoint.

Revision ID: 0022_reader_initial_import
Revises: 0021_reader_account_import
Create Date: 2026-10-06 11:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_reader_initial_import"
down_revision: str | None = "0021_reader_account_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("reader_account")}
    with op.batch_alter_table("reader_account") as batch:
        if "articles_per_sync" in columns and "initial_import_count" not in columns:
            batch.alter_column(
                "articles_per_sync", new_column_name="initial_import_count", server_default="200"
            )
        elif "initial_import_count" not in columns:
            batch.add_column(
                sa.Column(
                    "initial_import_count", sa.Integer(), nullable=False, server_default="200"
                )
            )
        if "sync_checkpoint" not in columns:
            batch.add_column(sa.Column("sync_checkpoint", sa.Integer(), nullable=True))
    # Legacy continuation tokens are meaningless to the checkpoint scheme.
    op.execute("UPDATE reader_account SET sync_cursor = NULL")


def downgrade() -> None:
    with op.batch_alter_table("reader_account") as batch:
        batch.drop_column("sync_checkpoint")
        batch.alter_column("initial_import_count", new_column_name="articles_per_sync")
