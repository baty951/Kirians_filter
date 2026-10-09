"""Persist captcha challenges and index archive cleanup.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""
from alembic import op
import sqlalchemy as sa

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "captcha_challenges",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("answer", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )
    op.create_index("ix_captcha_challenges_expires_at", "captcha_challenges", ["expires_at"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index("ix_action_logs_created_at", "action_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_action_logs_created_at", table_name="action_logs")
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_table("captcha_challenges")
