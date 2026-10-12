"""Mention filter and allowed link domains.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("filter_mentions", "warn_on_mentions"):
        op.add_column("chats", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
        op.alter_column("chats", name, server_default=None)
    op.add_column("chats", sa.Column("allowed_domains", sa.Text(), nullable=True))
    # The link filter used to block @mentions: keep that for chats that already use it.
    op.execute("UPDATE chats SET filter_mentions = filter_links, warn_on_mentions = warn_on_links")


def downgrade() -> None:
    op.drop_column("chats", "allowed_domains")
    op.drop_column("chats", "warn_on_mentions")
    op.drop_column("chats", "filter_mentions")
