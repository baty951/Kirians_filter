import re
from datetime import timedelta

from aiogram import Bot
from aiogram.types import Message, User
from aiogram.utils.markdown import hlink

from config import get_settings

# Matches "30m", "2h", "1d", "10s" → timedelta. Bare number = minutes.
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd]?)$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 60}


def mention(user: User) -> str:
    """HTML mention link for a user (works even without a username)."""
    return hlink(user.full_name, f"tg://user?id={user.id}")


def is_protected(user_id: int, bot: Bot) -> bool:
    """True if the user is immune to restrictions: the bot itself or any ID
    listed in PROTECTED_IDS."""
    return user_id == bot.id or user_id in get_settings().protected_ids


def parse_duration(text: str | None) -> timedelta | None:
    """Parse a human duration like '30m', '2h', '1d'. Returns None if unparseable."""
    if not text:
        return None
    match = _DURATION_RE.match(text.strip())
    if not match:
        return None
    value, unit = match.groups()
    return timedelta(seconds=int(value) * _UNIT_SECONDS[unit.lower()])


def extract_target_and_reason(message: Message) -> tuple[int | None, str | None]:
    """Resolve the moderation target from a reply or @mention/id argument.

    Returns (user_id, reason). Reply takes priority; otherwise the first
    command argument is treated as a numeric id, and the rest as the reason.
    """
    args = (message.text or "").split(maxsplit=2)[1:]

    if message.reply_to_message and message.reply_to_message.from_user:
        reason = " ".join(args) if args else None
        return message.reply_to_message.from_user.id, reason

    if args and args[0].lstrip("-").isdigit():
        reason = args[1] if len(args) > 1 else None
        return int(args[0]), reason

    return None, None
