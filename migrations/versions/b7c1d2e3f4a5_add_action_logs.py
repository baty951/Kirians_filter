"""add action_logs

Revision ID: b7c1d2e3f4a5
Revises: b2c3d4e5f6a7
Create Date: 2026-05-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c1d2e3f4a5'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('action_logs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('actor_id', sa.BigInteger(), nullable=True),
    sa.Column('target_id', sa.BigInteger(), nullable=True),
    sa.Column('reason', sa.String(length=512), nullable=True),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.chat_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_action_logs_chat_id'), 'action_logs', ['chat_id'], unique=False)
    op.create_index(op.f('ix_action_logs_target_id'), 'action_logs', ['target_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_action_logs_target_id'), table_name='action_logs')
    op.drop_index(op.f('ix_action_logs_chat_id'), table_name='action_logs')
    op.drop_table('action_logs')
