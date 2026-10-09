import asyncio

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from bot.database.crud import get_or_create_chat
from bot.utils.actions import MUTED_PERMISSIONS, unmute_user
from bot.utils.captcha import build_captcha
from bot.utils.helpers import mention

router = Router(name="welcome")


def _pending_key(chat_id: int, user_id: int) -> str:
    return f"captcha:{chat_id}:{user_id}"


@router.message(F.new_chat_members)
async def on_join(message: Message, bot: Bot, session: AsyncSession, redis: Redis) -> None:
    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    settings = get_settings()

    for user in message.new_chat_members:
        if user.is_bot:
            continue

        if not chat.captcha_enabled:
            if chat.welcome_enabled:
                text = chat.welcome_text or "👋 Добро пожаловать, {name}!"
                await message.answer(text.replace("{name}", mention(user)))
            continue

        # Mute until captcha is solved, then post the challenge.
        try:
            await bot.restrict_chat_member(message.chat.id, user.id, permissions=MUTED_PERMISSIONS)
        except Exception:
            pass

        question, keyboard = build_captcha(user.id)
        sent = await message.answer(f"{mention(user)}, {question}", reply_markup=keyboard)

        await redis.set(_pending_key(message.chat.id, user.id), sent.message_id, ex=settings.captcha_timeout_seconds + 5)
        asyncio.create_task(
            _expire_unsolved_captcha(bot, redis, message.chat.id, user.id, sent.message_id, settings.captcha_timeout_seconds)
        )


async def _expire_unsolved_captcha(
    bot: Bot, redis: Redis, chat_id: int, user_id: int, captcha_msg_id: int, timeout: int
) -> None:
    """On timeout, remove the challenge but leave the user muted (no kick).
    They can get a fresh captcha by leaving and rejoining the chat."""
    await asyncio.sleep(timeout)
    key = _pending_key(chat_id, user_id)
    if await redis.exists(key):
        await redis.delete(key)
        try:
            await bot.delete_message(chat_id, captcha_msg_id)
        except Exception:
            pass


@router.callback_query(F.data.startswith("captcha:"))
async def on_captcha_answer(callback: CallbackQuery, bot: Bot, redis: Redis) -> None:
    _, target_id, choice, answer = callback.data.split(":")

    # Only the user being challenged may press the buttons.
    if callback.from_user.id != int(target_id):
        await callback.answer("Это не ваша капча.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    key = _pending_key(chat_id, callback.from_user.id)

    if choice == answer:
        await redis.delete(key)
        await unmute_user(bot, chat_id, callback.from_user.id)
        await callback.message.delete()
        await callback.answer("✅ Добро пожаловать!")
    else:
        # Wrong answer: keep the user muted (do not kick). They stay in the chat
        # but cannot write until they pass a fresh captcha — obtained by leaving
        # and rejoining the chat.
        await redis.delete(key)
        await callback.message.delete()
        await callback.answer(
            "❌ Неверно. Вы останетесь без права писать. "
            "Выйдите из чата и зайдите снова, чтобы пройти проверку заново.",
            show_alert=True,
        )
