"""flag stories of already shared links as saved (data backfill, no schema change)

From now on every story containing an article of a user's "Shared with NewsGator" feed
(`saved:<user_id>`) is saved for that user. This marks the existing ones: a missing
story_state row is created unread, an existing row only gets `saved_at` (read state is
never touched). `saved_at` is the time the link was first shared (article.fetched_at).
The downgrade is a no-op: backfilled flags are indistinguishable from user-made ones.

Revision ID: 0025_saved_shared_links
Revises: 0024_story_presets
Create Date: 2026-10-22 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_saved_shared_links"
down_revision: str | None = "0024_story_presets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT f.url AS url, a.story_id AS story_id, MIN(a.fetched_at) AS shared_at "
            "FROM feed f JOIN article a ON a.feed_id = f.id "
            "WHERE f.kind = 'saved' AND a.story_id IS NOT NULL "
            "GROUP BY f.url, a.story_id"
        )
    ).all()
    owners = {r[0] for r in bind.execute(sa.text('SELECT id FROM "user"')).all()}
    for url, story_id, shared_at in rows:
        raw = str(url).removeprefix("saved:")
        if not raw.isdigit() or int(raw) not in owners:
            continue
        params = {"u": int(raw), "s": story_id, "t": shared_at}
        updated = bind.execute(
            sa.text(
                "UPDATE story_state SET saved_at = :t "
                "WHERE user_id = :u AND story_id = :s AND saved_at IS NULL"
            ),
            params,
        )
        exists = bind.execute(
            sa.text("SELECT 1 FROM story_state WHERE user_id = :u AND story_id = :s"), params
        ).first()
        if not updated.rowcount and exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO story_state (user_id, story_id, is_read, read_at_version, saved_at) "
                    "VALUES (:u, :s, 0, 0, :t)"
                ),
                params,
            )


def downgrade() -> None:
    pass
