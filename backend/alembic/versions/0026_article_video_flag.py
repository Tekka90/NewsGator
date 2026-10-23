"""persisted video flag on articles (VIDEO_ARTICLES_PLAN D2)

Adds article.media_kind (``"video"`` or NULL, indexed), video_url, video_embed_url,
video_provider (youtube|file|embed), video_id (YouTube only) and video_duration (seconds).
Existing articles whose URL is a YouTube video are backfilled with the same parser the
ingest path uses; nothing else is re-detected (D11).

Revision ID: 0026_article_video_flag
Revises: 0025_saved_shared_links
Create Date: 2026-10-23 07:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_article_video_flag"
down_revision: str | None = "0025_saved_shared_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 500


def upgrade() -> None:
    op.add_column("article", sa.Column("media_kind", sa.String(length=16), nullable=True))
    op.add_column("article", sa.Column("video_url", sa.String(length=2048), nullable=True))
    op.add_column("article", sa.Column("video_embed_url", sa.String(length=2048), nullable=True))
    op.add_column("article", sa.Column("video_provider", sa.String(length=16), nullable=True))
    op.add_column("article", sa.Column("video_id", sa.String(length=32), nullable=True))
    op.add_column("article", sa.Column("video_duration", sa.Integer(), nullable=True))
    op.create_index("ix_article_media_kind", "article", ["media_kind"])

    from app.services import youtube

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, url FROM article WHERE lower(url) LIKE '%youtu%'")
    ).all()
    updates = []
    for article_id, url in rows:
        video_id = youtube.parse_video_id(str(url))
        if video_id:
            updates.append(
                {
                    "i": article_id,
                    "u": f"{youtube.CANONICAL_BASE}{video_id}",
                    "e": youtube.embed_url(video_id),
                    "v": video_id,
                }
            )
    statement = sa.text(
        "UPDATE article SET media_kind = 'video', video_provider = 'youtube', "
        "video_url = :u, video_embed_url = :e, video_id = :v WHERE id = :i"
    )
    for start in range(0, len(updates), _BATCH):
        bind.execute(statement, updates[start : start + _BATCH])


def downgrade() -> None:
    op.drop_index("ix_article_media_kind", table_name="article")
    for column in (
        "video_duration",
        "video_id",
        "video_provider",
        "video_embed_url",
        "video_url",
        "media_kind",
    ):
        op.drop_column("article", column)
