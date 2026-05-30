from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import get_or_create_chat, update_chat_setting
from bot.database.models import Chat
from bot.middlewares.admin_check import is_user_admin

router = Router(name="settings")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

# field name -> human label for the toggle menu
TOGGLES: dict[str, str] = {
    "filter_badwords": "Фильтр слов",
    "filter_links": "Фильтр ссылок",
    "filter_media": "Фильтр медиа",
    "filter_language": "Фильтр языков",
    "antiflood_enabled": "Антифлуд",
    "captcha_enabled": "Капча",
    "welcome_enabled": "Приветствие",
}


def _build_keyboard(chat: Chat) -> InlineKeyboardMarkup:
    rows = []
    for field, label in TOGGLES.items():
        enabled = getattr(chat, field)
        mark = "✅" if enabled else "❌"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"set:{field}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.from_user is None or not await is_user_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Только для администраторов.")
        return
    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    await message.answer(
        f"⚙️ Настройки чата\nЛимит предупреждений: {chat.warn_limit}, мут: {chat.mute_minutes} мин.\n"
        "Нажмите, чтобы включить/выключить:",
        reply_markup=_build_keyboard(chat),
    )


@router.callback_query(F.data.startswith("set:"))
async def toggle_setting(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    chat_id = callback.message.chat.id
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("Только для администраторов.", show_alert=True)
        return

    field = callback.data.split(":", 1)[1]
    if field not in TOGGLES:
        await callback.answer()
        return

    chat = await get_or_create_chat(session, chat_id)
    await update_chat_setting(session, chat_id, field, not getattr(chat, field))
    chat = await get_or_create_chat(session, chat_id)  # refresh
    await callback.message.edit_reply_markup(reply_markup=_build_keyboard(chat))
    await callback.answer(f"{TOGGLES[field]}: {'вкл' if getattr(chat, field) else 'выкл'}")
