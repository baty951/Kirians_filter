from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import count_recent_actions, get_banned_words, get_or_create_chat
from bot.filters.content import contains_link, find_banned_word
from bot.filters.language import detect_blocked_script, parse_scripts
from bot.middlewares.admin_check import is_user_admin
from bot.utils.actions import log_action, mute_user, safe_call, warn_user
from bot.utils.helpers import is_protected, mention, restriction_lock


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
    reason = kind = None
    if chat.antiflood_enabled and flooding:
        reason, kind = "flood", "flood"
    elif chat.filter_badwords and text:
        hit = find_banned_word(text, await get_banned_words(session, message.chat.id))
        if hit:
            reason, kind = f"badword «{hit}»", "badwords"
    if reason is None and chat.filter_links and contains_link(
        text, [*(message.entities or []), *(message.caption_entities or [])],
    ):
        reason, kind = "link", "links"
    if reason is None and chat.filter_media and any(getattr(message, field, None) for field in (
        "photo", "video", "animation", "sticker", "document", "audio", "voice",
        "video_note", "paid_media", "story", "live_photo",
    )):
        reason, kind = "media", "media"
    if reason is None and chat.filter_language and text and chat.blocked_scripts:
        script = detect_blocked_script(text, parse_scripts(chat.blocked_scripts))
        if script:
            reason, kind = f"lang «{script}»", "language"
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
    warn = (sender is None and message.chat.type != ChatType.CHANNEL
            and getattr(chat, f"warn_on_{kind}"))  # channels / anonymous admins: delete-only
    outcome = None
    # One flood/warn pipeline per user: later flagged messages must not re-apply a shorter mute.
    async with restriction_lock(message.chat.id, target_id, "flood"):
        # Messages sent before the mute takes effect are still flagged; don't override escalation.
        if reason == "flood" and not await count_recent_actions(
            session, message.chat.id, target_id,
            # A shorter recent warn-mute must not suppress the (longer) flood mute.
            ("FLOOD_MUTE", "WARN_BAN") + (("WARN_MUTE",) if chat.mute_minutes >= chat.flood_mute_minutes else ()),
            datetime.now(timezone.utc) - timedelta(seconds=60), actor_id=bot.id,
        ):
            action = "FLOOD_MUTE" if sender is None else "FLOOD_BAN"
            if sender is None:
                error = await mute_user(
                    bot, message.chat.id, target_id, timedelta(minutes=chat.flood_mute_minutes), session=session,
                )
            else:
                error = await safe_call(bot.ban_chat_sender_chat(message.chat.id, target_id))
            await log_action(
                bot, session, message.chat.id,
                f"{action + '_FAILED' if error else action}: user {target_id}"
                + (f", ошибка: {error}" if error else ""),
                action=action + "_FAILED" if error else action, actor_id=bot.id,
                target_id=target_id, reason="flood", content=error,
            )
        if warn:
            outcome = await warn_user(
                bot, session, message.chat.id, target_id, actor_id=bot.id, reason=reason,
            )
    if outcome and outcome[0] != "SKIP" and not outcome[2]:
        action, total, _ = outcome
        who = mention(message.from_user)
        await safe_call(message.answer({
            "WARN": f"⚠️ {who}, предупреждение {total}/{chat.warn_limit}.",
            "WARN_MUTE": f"🔇 {who} заглушен за повторные нарушения.",
            "WARN_BAN": f"🔨 {who} забанен за повторные нарушения.",
        }[action]))
    return True
