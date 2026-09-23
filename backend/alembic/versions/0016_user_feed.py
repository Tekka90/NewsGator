"""per-user feed subscriptions (user_feed)

Revision ID: 0016_user_feed
Revises: 0015_reader_account
Create Date: 2026-09-23

Per-user feed subscriptions (user_feed join table). Feeds, articles, summaries,
embeddings, and stories remain global and deduplicated. Users manage their own
subscriptions; story lists are scoped to stories having >= 1 article from the
user's subscribed feeds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_user_feed"
down_revision: str | None = "0015_reader_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_feed",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), primary_key=True),
        sa.Column("feed_id", sa.Integer(), sa.ForeignKey("feed.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Backfill: subscribe all existing users to all existing feeds
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO user_feed (user_id, feed_id, created_at) "
            "SELECT user.id, feed.id, CURRENT_TIMESTAMP FROM user CROSS JOIN feed"
        )
    )


def downgrade() -> None:
    op.drop_table("user_feed")
