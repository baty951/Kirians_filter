"""Chat rules text.

Revision ID: k7f8a9b0c1d2
Revises: j6e7f8a9b0c1
"""
from alembic import op
import sqlalchemy as sa

revision = "k7f8a9b0c1d2"
down_revision = "j6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("rules_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chats", "rules_text")
