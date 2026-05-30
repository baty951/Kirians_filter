"""add language filter

Revision ID: b2c3d4e5f6a7
Revises: 9a87f5d90b91
Create Date: 2026-05-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "9a87f5d90b91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add with a server_default so existing rows get a value, then drop it to
    # match the model (which has only a Python-side default).
    op.add_column(
        "chats",
        sa.Column("filter_language", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("chats", "filter_language", server_default=None)
    op.add_column("chats", sa.Column("blocked_scripts", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("chats", "blocked_scripts")
    op.drop_column("chats", "filter_language")
