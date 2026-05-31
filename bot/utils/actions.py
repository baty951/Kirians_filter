from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import ChatPermissions

from bot.database.crud import add_log, get_or_create_chat
from sqlalchemy.ext.asyncio import AsyncSession

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


async def mute_user(bot: Bot, chat_id: int, user_id: int, until: timedelta | None) -> None:
    until_date = datetime.now(timezone.utc) + until if until else None
    await bot.restrict_chat_member(
        chat_id, user_id, permissions=MUTED_PERMISSIONS, until_date=until_date
    )


async def unmute_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(chat_id, user_id, permissions=UNMUTED_PERMISSIONS)


async def ban_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id, user_id)


async def unban_user(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def kick_user(bot: Bot, chat_id: int, user_id: int) -> None:
    """Kick = ban then immediately unban so the user can rejoin."""
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


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
