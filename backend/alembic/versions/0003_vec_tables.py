"""article embeddings (vec0 virtual tables)

Revision ID: 0003_vec_tables
Revises: 0002_feed_empty_polls
Create Date: 2026-08-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_vec_tables"
down_revision: str | None = "0002_feed_empty_polls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024


def upgrade() -> None:
    # vec0 virtual tables can't be created via SQLAlchemy DDL — raw SQL.
    # Guarded: sqlite-vec may be absent in minimal/test environments; the app
    # falls back to the in-memory store there.
    conn = op.get_bind()
    try:
        import sqlite_vec

        sqlite_vec.load(conn)
    except Exception:
        return
    conn.exec_driver_sql(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_article "
        f"USING vec0(embedding float[{EMBED_DIM}])"
    )
    conn.exec_driver_sql(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_story "
        f"USING vec0(embedding float[{EMBED_DIM}])"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP TABLE IF EXISTS vec_article")
    conn.exec_driver_sql("DROP TABLE IF EXISTS vec_story")
