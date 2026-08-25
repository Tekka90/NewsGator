"""image_url on article + story

Revision ID: 0005_image_urls
Revises: 0004_cluster_tables
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_image_urls"
down_revision: str | None = "0004_cluster_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("article", sa.Column("image_url", sa.String(2048), nullable=True))
    op.add_column("story", sa.Column("image_url", sa.String(2048), nullable=True))


def downgrade() -> None:
    op.drop_column("story", "image_url")
    op.drop_column("article", "image_url")
