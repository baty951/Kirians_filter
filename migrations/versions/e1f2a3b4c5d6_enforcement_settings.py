"""Per-chat warn escalation, warn TTL and anti-flood settings.

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
"""
from alembic import op
import sqlalchemy as sa

from config import get_settings

revision = "e1f2a3b4c5d6"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None

_BOOLS = ("warn_on_badwords", "warn_on_links", "warn_on_media", "warn_on_language", "warn_on_flood")


def upgrade() -> None:
    settings = get_settings()
    ints = {
        "warn_ttl_days": settings.default_warn_ttl_days,
        "ban_after_mutes": settings.default_ban_after_mutes,
        "flood_mute_minutes": settings.default_flood_mute_minutes,
        # Existing chats keep the values the running process was already using.
        "antiflood_messages": settings.antiflood_messages,
        "antiflood_window_seconds": settings.antiflood_window_seconds,
    }
    # server_default only backfills existing rows; it is dropped right after (model has Python-side defaults).
    for name in _BOOLS:
        op.add_column("chats", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
        op.alter_column("chats", name, server_default=None)
    for name, value in ints.items():
        op.add_column("chats", sa.Column(name, sa.Integer(), nullable=False, server_default=str(value)))
        op.alter_column("chats", name, server_default=None)


def downgrade() -> None:
    for name in ("antiflood_window_seconds", "antiflood_messages", "flood_mute_minutes",
                 "ban_after_mutes", "warn_ttl_days", *reversed(_BOOLS)):
        op.drop_column("chats", name)
