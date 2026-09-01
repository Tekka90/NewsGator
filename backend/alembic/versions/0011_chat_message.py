"""chat message table (server-side chat history)

Revision ID: 0011_chat_message
Revises: 0010_llm_usage
Create Date: 2026-09-01

One row per chat turn (user question / assistant answer / error), per user, so
history follows the user across devices. Append-only; story citations are
denormalized into stories_json so history survives story retention/deletion.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_chat_message"
down_revision: str | None = "0010_llm_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("stories_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_message_user_id", "chat_message", ["user_id"])
    op.create_index("ix_chat_message_created_at", "chat_message", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_message_created_at", table_name="chat_message")
    op.drop_index("ix_chat_message_user_id", table_name="chat_message")
    op.drop_table("chat_message")
