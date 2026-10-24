"""user.ui_language (web interface language)

Revision ID: 0031_user_ui_language
Revises: 0030_listen_position_validity
Create Date: 2026-10-24 13:00:00.000000

Empty string = follow the browser language. Interface chrome only; content language
stays `summary_language`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_user_ui_language"
down_revision: str | None = "0030_listen_position_validity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.add_column(
            sa.Column("ui_language", sa.String(length=8), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.drop_column("ui_language")
