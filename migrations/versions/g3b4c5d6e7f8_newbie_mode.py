"""Newbie mode: member join times and chat settings.

Revision ID: g3b4c5d6e7f8
Revises: f2a3b4c5d6e7
"""
from alembic import op
import sqlalchemy as sa

revision = "g3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("filter_newbie", "warn_on_newbie"):
        op.add_column("chats", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
        op.alter_column("chats", name, server_default=None)
    # server_default only backfills existing rows; it is dropped right after.
    op.add_column("chats", sa.Column("newbie_hours", sa.Integer(), nullable=False, server_default="24"))
    op.alter_column("chats", "newbie_hours", server_default=None)
    op.create_table(
        "member_joins",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )
    op.create_index("ix_member_joins_joined_at", "member_joins", ["joined_at"])


def downgrade() -> None:
    op.drop_index("ix_member_joins_joined_at", table_name="member_joins")
    op.drop_table("member_joins")
    op.drop_column("chats", "newbie_hours")
    op.drop_column("chats", "warn_on_newbie")
    op.drop_column("chats", "filter_newbie")
