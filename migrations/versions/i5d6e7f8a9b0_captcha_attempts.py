"""Captcha attempts.

Revision ID: i5d6e7f8a9b0
Revises: h4c5d6e7f8a9
"""
from alembic import op
import sqlalchemy as sa

revision = "i5d6e7f8a9b0"
down_revision = "h4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default only backfills existing rows; it is dropped right after.
    op.add_column("chats", sa.Column("captcha_attempts", sa.Integer(), nullable=False, server_default="3"))
    op.alter_column("chats", "captcha_attempts", server_default=None)
    # Challenges already in flight keep the old single-attempt behaviour.
    op.add_column("captcha_challenges", sa.Column("attempts_left", sa.Integer(), nullable=False, server_default="1"))
    op.alter_column("captcha_challenges", "attempts_left", server_default=None)


def downgrade() -> None:
    op.drop_column("captcha_challenges", "attempts_left")
    op.drop_column("chats", "captcha_attempts")
