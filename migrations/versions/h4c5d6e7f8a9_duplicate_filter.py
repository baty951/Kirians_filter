"""Duplicate message filter settings.

Revision ID: h4c5d6e7f8a9
Revises: g3b4c5d6e7f8
"""
from alembic import op
import sqlalchemy as sa

revision = "h4c5d6e7f8a9"
down_revision = "g3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("filter_duplicates", "warn_on_duplicates"):
        op.add_column("chats", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
        op.alter_column("chats", name, server_default=None)
    # server_default only backfills existing rows; it is dropped right after.
    op.add_column("chats", sa.Column("duplicate_window_seconds", sa.Integer(), nullable=False, server_default="300"))
    op.alter_column("chats", "duplicate_window_seconds", server_default=None)


def downgrade() -> None:
    op.drop_column("chats", "duplicate_window_seconds")
    op.drop_column("chats", "warn_on_duplicates")
    op.drop_column("chats", "filter_duplicates")
