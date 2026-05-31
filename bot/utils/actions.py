import logging
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions

from bot.database.crud import add_log, get_or_create_chat
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Maps recurring Telegram API error fragments to a human-readable Russian hint.
_ERROR_HINTS = {
    "remove chat owner": "нельзя ограничить владельца чата",
    "user is an administrator": "нельзя ограничить администратора чата",
    "not enough rights": "у бота недостаточно прав",
    "have no rights": "у бота недостаточно прав",
    "chat_admin_required": "у бота недостаточно прав",
    "user_not_participant": "пользователь не состоит в чате",
    "user not found": "пользователь не найден",
    "method is available only for supergroups": "доступно только в супергруппах",
    "can't restrict self": "бот не может ограничить сам себя",
}


def humanize_error(description: str | None) -> str:
    """Turn a raw Telegram error description into a friendly Russian phrase."""
    if not description:
        return "неизвестная ошибка"
    low = description.lower()
    for fragment, hint in _ERROR_HINTS.items():
        if fragment in low:
            return hint
    return description


async def _safe_call(action: Awaitable[object]) -> str | None:
    """Await a Telegram action, swallowing API errors.

    Returns None on success, or the error description on failure (also logged),
    so callers can inform the user instead of crashing the handler.
    """
    try:
        await action
        return None
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Telegram action failed: %s", exc.message)
        return exc.message

MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

UNMUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def mute_user(bot: Bot, chat_id: int, user_id: int, until: timedelta | None) -> str | None:
    until_date = datetime.now(timezone.utc) + until if until else None
    return await _safe_call(
        bot.restrict_chat_member(
            chat_id, user_id, permissions=MUTED_PERMISSIONS, until_date=until_date
        )
    )


async def unmute_user(bot: Bot, chat_id: int, user_id: int) -> str | None:
    return await _safe_call(
        bot.restrict_chat_member(chat_id, user_id, permissions=UNMUTED_PERMISSIONS)
    )


async def ban_user(bot: Bot, chat_id: int, user_id: int) -> str | None:
    return await _safe_call(bot.ban_chat_member(chat_id, user_id))


async def unban_user(bot: Bot, chat_id: int, user_id: int) -> str | None:
    return await _safe_call(bot.unban_chat_member(chat_id, user_id, only_if_banned=True))


async def kick_user(bot: Bot, chat_id: int, user_id: int) -> str | None:
    """Kick = ban then immediately unban so the user can rejoin."""
    err = await _safe_call(bot.ban_chat_member(chat_id, user_id))
    if err:
        return err
    return await _safe_call(bot.unban_chat_member(chat_id, user_id, only_if_banned=True))


async def log_action(
    bot: Bot,
    session: AsyncSession,
    chat_id: int,
    text: str,
    *,
    action: str | None = None,
    actor_id: int | None = None,
    target_id: int | None = None,
    reason: str | None = None,
    content: str | None = None,
) -> None:
    """Record a moderation event.

    When ``action`` is given, a structured row is persisted to the
    ``action_logs`` table. Regardless, ``text`` is mirrored to the chat's
    configured log channel, if any.
    """
    if action is not None:
        await add_log(
            session,
            chat_id,
            action,
            actor_id=actor_id,
            target_id=target_id,
            reason=reason,
            content=content,
        )
    chat = await get_or_create_chat(session, chat_id)
    if chat.log_channel_id:
        try:
            await bot.send_message(chat.log_channel_id, text)
        except Exception:
            pass
