from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import get_banned_words, get_or_create_chat
from bot.filters.content import contains_link, find_banned_word
from bot.filters.language import detect_blocked_script, parse_scripts
from bot.middlewares.admin_check import is_user_admin
from bot.utils.actions import log_action, mute_user
from bot.utils.helpers import is_protected, parse_duration

router = Router(name="moderation")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


@router.message()
async def auto_moderate(
    message: Message, bot: Bot, session: AsyncSession, flooding: bool = False
) -> None:
    """Inspect every group message against the chat's enabled filters.

    Admins are exempt. Triggers delete the offending message and, for flood,
    apply a temporary mute.
    """
    if message.from_user is None or message.from_user.is_bot:
        return
    if is_protected(message.from_user.id, bot):
        return
    if await is_user_admin(bot, message.chat.id, message.from_user.id):
        return

    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)

    # 1. Anti-flood (flagged by AntiFloodMiddleware via Redis counter).
    if chat.antiflood_enabled and flooding:
        await _delete(message)
        await mute_user(bot, message.chat.id, message.from_user.id, parse_duration("10m"))
        await log_action(bot, session, message.chat.id, f"FLOOD→MUTE: user {message.from_user.id}")
        return

    text = message.text or message.caption or ""

    # 2. Banned words.
    if chat.filter_badwords and text:
        words = await get_banned_words(session, message.chat.id)
        hit = find_banned_word(text, words)
        if hit:
            await _delete(message)
            await log_action(bot, session, message.chat.id, f"BADWORD «{hit}»: user {message.from_user.id}")
            return

    # 3. Links.
    if chat.filter_links and text and contains_link(text):
        await _delete(message)
        await log_action(bot, session, message.chat.id, f"LINK: user {message.from_user.id}")
        return

    # 4. Media restriction.
    if chat.filter_media and (message.photo or message.video or message.animation or message.sticker):
        await _delete(message)
        await log_action(bot, session, message.chat.id, f"MEDIA: user {message.from_user.id}")
        return

    # 5. Language / script filter.
    if chat.filter_language and text and chat.blocked_scripts:
        script = detect_blocked_script(text, parse_scripts(chat.blocked_scripts))
        if script:
            await _delete(message)
            await log_action(bot, session, message.chat.id, f"LANG «{script}»: user {message.from_user.id}")
            return


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
