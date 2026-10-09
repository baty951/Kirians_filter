from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import get_banned_words, get_or_create_chat
from bot.filters.content import contains_link, find_banned_word
from bot.filters.language import detect_blocked_script, parse_scripts
from bot.middlewares.admin_check import is_user_admin
from bot.utils.actions import log_action, mute_user, safe_call
from bot.utils.helpers import is_protected, parse_duration


async def auto_moderate(
    message: Message, bot: Bot, session: AsyncSession, flooding: bool = False,
) -> bool:
    """Return True when a forbidden message is consumed, even if deletion fails."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return False
    # Joins must reach the captcha handler; auto-forwards come from the linked channel.
    if message.new_chat_members or message.left_chat_member or message.is_automatic_forward:
        return False
    sender = message.sender_chat
    if sender is not None:
        if sender.id == message.chat.id and message.chat.type != ChatType.CHANNEL:
            return False  # Telegram's anonymous group administrator.
        if (message.chat.type == ChatType.CHANNEL and sender.id == message.chat.id
                and any(e.type == "bot_command" and e.offset == 0 for e in message.entities or [])):
            return False  # Channel publishers may configure the bot with commands.
        target_id = sender.id
    else:
        user = message.from_user
        if user is None or user.is_bot or is_protected(user.id, bot):
            return False
        if message.chat.type != ChatType.CHANNEL and await is_user_admin(bot, message.chat.id, user.id):
            return False
        target_id = user.id

    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    text = message.text or message.caption or ""
    reason = None
    if chat.antiflood_enabled and flooding:
        reason = "flood"
    elif chat.filter_badwords and text:
        hit = find_banned_word(text, await get_banned_words(session, message.chat.id))
        if hit:
            reason = f"badword «{hit}»"
    if reason is None and chat.filter_links and contains_link(
        text, [*(message.entities or []), *(message.caption_entities or [])],
    ):
        reason = "link"
    if reason is None and chat.filter_media and any(getattr(message, field, None) for field in (
        "photo", "video", "animation", "sticker", "document", "audio", "voice",
        "video_note", "paid_media", "story", "live_photo",
    )):
        reason = "media"
    if reason is None and chat.filter_language and text and chat.blocked_scripts:
        script = detect_blocked_script(text, parse_scripts(chat.blocked_scripts))
        if script:
            reason = f"lang «{script}»"
    if reason is None:
        return False

    error = await safe_call(message.delete())
    await log_action(
        bot, session, message.chat.id,
        f"{'DELETE_FAILED' if error else 'DELETE'}: user {target_id}, {reason}"
        + (f", ошибка: {error}" if error else ""),
        action="DELETE_FAILED" if error else "DELETE", actor_id=bot.id,
        target_id=target_id, reason=reason, content=text or None,
    )
    if reason == "flood":
        action = "FLOOD_MUTE" if sender is None else "FLOOD_BAN"
        if sender is None:
            error = await mute_user(bot, message.chat.id, target_id, parse_duration("10m"), session=session)
        else:
            error = await safe_call(bot.ban_chat_sender_chat(message.chat.id, target_id))
        await log_action(
            bot, session, message.chat.id,
            f"{action + '_FAILED' if error else action}: user {target_id}"
            + (f", ошибка: {error}" if error else ""),
            action=action + "_FAILED" if error else action, actor_id=bot.id,
            target_id=target_id, reason="flood", content=error,
        )
    return True
