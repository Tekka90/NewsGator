"""Topic Radar: topic, story_topic, topic_rule, story_rule_match, topic_feedback

Revision ID: 0032_topic_radar
Revises: 0031_user_ui_language
Create Date: 2026-11-21 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_topic_radar"
down_revision: str | None = "0031_user_ui_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(256), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("label_language", sa.String(8), nullable=False, server_default=""),
        sa.Column("centroid", sa.LargeBinary(), nullable=False),
        sa.Column("story_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_story_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.create_index("ix_topic_status", "topic", ["status"])
    op.create_table(
        "story_topic",
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topic.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("similarity", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_story_topic_topic_id", "story_topic", ["topic_id"])
    op.create_table(
        "topic_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("label", sa.String(256), nullable=False, server_default=""),
        sa.Column("centroid", sa.LargeBinary(), nullable=False),
        sa.Column("embed_model", sa.String(128), nullable=False, server_default=""),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("source_topic_id", sa.Integer(), nullable=True),
        sa.Column("until_fade", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_topic_rule_user_id", "topic_rule", ["user_id"])
    op.create_table(
        "story_rule_match",
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("topic_rule.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("story.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
    )
    op.create_index("ix_story_rule_match_story_id", "story_rule_match", ["story_id"])
    op.create_index("ix_story_rule_match_user_id", "story_rule_match", ["user_id"])
    op.create_table(
        "topic_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("story_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("topic_feedback", "story_rule_match", "topic_rule", "story_topic", "topic"):
        op.drop_table(table)
