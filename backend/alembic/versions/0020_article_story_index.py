"""Index article.story_id and (feed_id, story_id).

The story list, stats and source-host lookups filter articles by story; without an
index each lookup read through the large text columns of every article row.

Revision ID: 0020_article_story_index
Revises: 0019_story_saved
Create Date: 2026-10-01 14:50:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_article_story_index"
down_revision: str | None = "0019_story_saved"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # databases created from the models (legacy create_all) already carry these indexes
    op.create_index("ix_article_story_id", "article", ["story_id"], if_not_exists=True)
    op.create_index("ix_article_feed_story", "article", ["feed_id", "story_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_article_feed_story", table_name="article", if_exists=True)
    op.drop_index("ix_article_story_id", table_name="article", if_exists=True)
