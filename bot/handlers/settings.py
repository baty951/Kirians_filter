from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import get_or_create_chat, update_chat_setting
from bot.database.models import Chat
from bot.middlewares.admin_check import is_user_admin
from bot.handlers.admin import _guard
from bot.utils.actions import safe_call

MENU_TEXT = "⚙️ Настройки чата\nВыберите раздел:"

router = Router(name="settings")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}))

# field name -> human label for the toggle menu
TOGGLES: dict[str, str] = {
    "filter_badwords": "Фильтр слов",
    "filter_links": "Фильтр ссылок",
    "filter_mentions": "Фильтр @упоминаний",
    "filter_media": "Фильтр медиа",
    "filter_newbie": "Режим новичка",
    "filter_duplicates": "Фильтр дубликатов",
    "filter_language": "Фильтр языков",
    "antiflood_enabled": "Антифлуд",
    "captcha_enabled": "Капча",
    "welcome_enabled": "Приветствие",
    "warn_on_badwords": "Варн: слова",
    "warn_on_links": "Варн: ссылки",
    "warn_on_mentions": "Варн: упоминания",
    "warn_on_media": "Варн: медиа",
    "warn_on_newbie": "Варн: новичок",
    "warn_on_duplicates": "Варн: дубликаты",
    "warn_on_language": "Варн: языки",
    "warn_on_flood": "Варн: флуд",
}

# field -> (label, ladder of allowed values stepped by the +/- buttons)
NUMBERS: dict[str, tuple[str, tuple[int, ...]]] = {
    "warn_limit": ("Лимит варнов", tuple(range(1, 11))),
    "mute_minutes": ("Мут, мин", (1, 5, 10, 15, 30, 60, 120, 360, 720, 1440, 4320, 10080, 43200, 525600)),
    "captcha_attempts": ("Капча: попыток", (1, 2, 3, 4, 5)),
    "newbie_hours": ("Новичок, ч", (1, 6, 12, 24, 48, 72, 168)),
    "duplicate_window_seconds": ("Дубликаты: окно, с", (30, 60, 120, 300, 600, 1800, 3600)),
    "warn_ttl_days": ("Срок варна, дн", (1, 3, 7, 14, 30, 60, 90, 180, 365)),
    "ban_after_mutes": ("Мутов до бана", tuple(range(1, 11))),
    "antiflood_messages": ("Флуд: сообщений", (*range(2, 11), 15, 20, 30)),
    "antiflood_window_seconds": ("Флуд: окно, с", (1, 2, 3, 5, 10, 15, 30, 60)),
    "flood_mute_minutes": ("Мут за флуд, мин", (1, 5, 10, 15, 30, 60, 120, 360, 1440)),
}


# section key -> (title, fields shown in it); every TOGGLES / NUMBERS field belongs to exactly one section
SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "filters": ("🧹 Фильтры", (
        "filter_badwords", "filter_links", "filter_mentions", "filter_media", "filter_language",
        "filter_duplicates", "duplicate_window_seconds",
    )),
    "punish": ("⚠️ Наказания", (
        "warn_limit", "mute_minutes", "warn_ttl_days", "ban_after_mutes",
        "warn_on_badwords", "warn_on_links", "warn_on_mentions", "warn_on_media", "warn_on_language",
        "warn_on_flood", "warn_on_newbie", "warn_on_duplicates",
    )),
    "flood": ("🌊 Антифлуд", (
        "antiflood_enabled", "antiflood_messages", "antiflood_window_seconds", "flood_mute_minutes",
    )),
    "members": ("👋 Новички и капча", (
        "captcha_enabled", "captcha_attempts", "welcome_enabled", "filter_newbie", "newbie_hours",
    )),
}
_SECTION_OF = {field: key for key, (_, fields) in SECTIONS.items() for field in fields}


def _build_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=f"menu:{key}")] for key, (title, _) in SECTIONS.items()
    ])


def _build_keyboard(chat: Chat, section: str) -> InlineKeyboardMarkup:
    rows = []
    for field in SECTIONS[section][1]:
        if field in TOGGLES:
            mark = "✅" if getattr(chat, field) else "❌"
            rows.append([InlineKeyboardButton(text=f"{mark} {TOGGLES[field]}", callback_data=f"set:{field}")])
        else:
            label = NUMBERS[field][0]
            rows.append([
                InlineKeyboardButton(text="−", callback_data=f"num:{field}:-"),
                InlineKeyboardButton(text=f"{label}: {getattr(chat, field)}", callback_data=f"num:{field}:."),
                InlineKeyboardButton(text="+", callback_data=f"num:{field}:+"),
            ])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.channel_post(Command("settings"))
@router.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    await get_or_create_chat(session, message.chat.id, message.chat.title)
    await message.answer(MENU_TEXT, reply_markup=_build_menu())


@router.callback_query(F.data.startswith("menu:"))
async def open_menu(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Меню недоступно.")
        return
    if not await is_user_admin(bot, callback.message.chat.id, callback.from_user.id):
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key == "main":
        await safe_call(callback.message.edit_text(MENU_TEXT, reply_markup=_build_menu()))
    elif key in SECTIONS:
        chat = await get_or_create_chat(session, callback.message.chat.id)
        await safe_call(callback.message.edit_text(
            f"{SECTIONS[key][0]}\nПереключатели и значения (− / +):", reply_markup=_build_keyboard(chat, key),
        ))
    await callback.answer()


@router.callback_query(F.data.startswith("set:"))
async def toggle_setting(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Меню недоступно.")
        return
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
    await callback.message.edit_reply_markup(reply_markup=_build_keyboard(chat, _SECTION_OF[field]))
    await callback.answer(f"{TOGGLES[field]}: {'вкл' if getattr(chat, field) else 'выкл'}")


@router.callback_query(F.data.startswith("num:"))
async def step_setting(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Меню недоступно.")
        return
    chat_id = callback.message.chat.id
    if not await is_user_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("Только для администраторов.", show_alert=True)
        return

    _, field, step = (callback.data.split(":") + ["", ""])[:3]
    if field not in NUMBERS or step not in ("+", "-"):
        await callback.answer()  # includes the "." label button
        return

    chat = await get_or_create_chat(session, chat_id)
    current = getattr(chat, field)
    ladder = NUMBERS[field][1]
    # Step to the nearest ladder value, so values not on the ladder (from .env) still work.
    options = [v for v in ladder if (v > current if step == "+" else v < current)]
    if not options:
        # Editing to an identical keyboard makes Telegram raise "message is not modified".
        await callback.answer(f"{NUMBERS[field][0]}: предел")
        return
    value = min(options) if step == "+" else max(options)
    await update_chat_setting(session, chat_id, field, value)
    chat = await get_or_create_chat(session, chat_id)
    await callback.message.edit_reply_markup(reply_markup=_build_keyboard(chat, _SECTION_OF[field]))
    await callback.answer(f"{NUMBERS[field][0]}: {getattr(chat, field)}")


@router.channel_post(Command("setwelcome"))
@router.message(Command("setwelcome"))
async def cmd_setwelcome(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    text = escape(parts[1].strip(), quote=False) if len(parts) > 1 else None
    if text and len(text) > 1024:
        await message.reply("Текст слишком длинный: максимум 1024 символа (с учётом экранирования).")
        return
    await update_chat_setting(session, message.chat.id, "welcome_text", text or None)
    note = ""
    if text and message.chat.type == ChatType.SUPERGROUP and (await get_or_create_chat(session, message.chat.id)).captcha_enabled:
        note = "\nℹ️ Приветствие показывается только при выключенной капче."
    await message.reply(("👋 Приветствие обновлено." if text else "Приветствие сброшено на стандартное.") + note)
