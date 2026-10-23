"""story presets: source kind ``youtube`` becomes ``video`` (VIDEO_ARTICLES_PLAN D8)

Rewrites every stored preset document (``user.story_presets``): ``youtube`` in a preset's
``criteria.source_kinds`` is replaced by ``video`` (deduplicated when both are present, order
kept). The downgrade maps ``video`` back to ``youtube``.

Revision ID: 0027_presets_video_kind
Revises: 0026_article_video_flag
Create Date: 2026-10-23 07:05:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_presets_video_kind"
down_revision: str | None = "0026_article_video_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename(old: str, new: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text('SELECT id, story_presets FROM "user" WHERE story_presets IS NOT NULL')
    ).all()
    for user_id, stored in rows:
        try:
            presets = json.loads(stored)
        except (TypeError, ValueError):
            continue
        if not isinstance(presets, list):
            continue
        changed = False
        for preset in presets:
            criteria = preset.get("criteria") if isinstance(preset, dict) else None
            kinds = criteria.get("source_kinds") if isinstance(criteria, dict) else None
            if not isinstance(kinds, list) or old not in kinds:
                continue
            renamed: list[object] = []
            for kind in kinds:
                mapped = new if kind == old else kind
                if mapped not in renamed:
                    renamed.append(mapped)
            criteria["source_kinds"] = renamed
            changed = True
        if changed:
            bind.execute(
                sa.text('UPDATE "user" SET story_presets = :p WHERE id = :i'),
                {"p": json.dumps(presets, ensure_ascii=False, separators=(",", ":")), "i": user_id},
            )


def upgrade() -> None:
    _rename("youtube", "video")


def downgrade() -> None:
    _rename("video", "youtube")
