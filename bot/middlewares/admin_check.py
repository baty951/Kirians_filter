from aiogram import Bot
from aiogram.types import Chat


async def is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Return True if the user is an administrator or the creator of the chat."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in ("administrator", "creator")


async def is_bot_admin(bot: Bot, chat: Chat) -> bool:
    me = await bot.get_me()
    return await is_user_admin(bot, chat.id, me.id)
