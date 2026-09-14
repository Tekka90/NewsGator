"""feed.email_count: newsletter emails ingested per mail feed

Revision ID: 0013_feed_email_count
Revises: 0012_newsletter_mail
Create Date: 2026-09-14

Mail feeds only: incremented once per processed message so admins can spot a
stalled/broken newsletter feed (e.g. a sender that stopped being ingested).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_feed_email_count"
down_revision: str | None = "0012_newsletter_mail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feed",
        sa.Column("email_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("feed", "email_count")
