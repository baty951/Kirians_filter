"""Approved (trusted) users.

Revision ID: j6e7f8a9b0c1
Revises: i5d6e7f8a9b0
"""
from alembic import op
import sqlalchemy as sa

revision = "j6e7f8a9b0c1"
down_revision = "i5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approved_users",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("approved_users")
