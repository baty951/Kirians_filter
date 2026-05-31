from functools import lru_cache
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import get_banned_words
from bot.middlewares.admin_check import is_user_admin
from bot.utils.helpers import mention

router = Router(name="badwords")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

BADWORDS_DIR = Path(__file__).resolve().parents[2] / "data" / "badwords"

# Display name (with flag) per language file code; also defines button order.
LANG_NAMES: dict[str, str] = {
    "en": "🇬🇧 English",
    "ru": "🇷🇺 Русский",
    "es": "🇪🇸 Español",
    "de": "🇩🇪 Deutsch",
    "fr": "🇫🇷 Français",
    "it": "🇮🇹 Italiano",
    "pt": "🇵🇹 Português",
    "tr": "🇹🇷 Türkçe",
    "ar": "🇸🇦 العربية",
}

# Telegram hard limit is 4096; keep margin for headers/markup.
_CHUNK_LIMIT = 3500


@lru_cache(maxsize=32)
def _load_lang_words(lang: str) -> frozenset[str]:
    """Stems listed in data/badwords/<lang>.txt (comments/blank lines skipped)."""
    path = BADWORDS_DIR / f"{lang}.txt"
    if not path.is_file():
        return frozenset()
    words = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    return frozenset(words)


def _available_langs() -> list[str]:
    return [code for code in LANG_NAMES if (BADWORDS_DIR / f"{code}.txt").is_file()]


def _languages_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code in _available_langs():
        row.append(InlineKeyboardButton(text=LANG_NAMES[code], callback_data=f"bwlang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _split_messages(header: str, items: list[str]) -> list[str]:
    """Pack `header` + one-per-line items into Telegram-sized messages."""
    messages: list[str] = []
    current = header
    for item in items:
        addition = ("\n" if current else "") + item
        if len(current) + len(addition) > _CHUNK_LIMIT:
            messages.append(current)
            current = item
        else:
            current += addition
    if current:
        messages.append(current)
    return messages


async def _send_dm(bot: Bot, user_id: int, messages: list[str]) -> bool:
    """Try to DM the user. Returns False if the bot may not message them."""
    try:
        for text in messages:
            await bot.send_message(user_id, text)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


@router.message(Command("badwords", "words"))
async def cmd_badwords(message: Message) -> None:
    await message.answer(
        "🌐 Выберите язык запрещённых слов — список придёт вам в личные сообщения:",
        reply_markup=_languages_keyboard(),
    )


@router.callback_query(F.data.startswith("bwlang:"))
async def on_lang_chosen(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    lang = callback.data.split(":", 1)[1]
    chat = callback.message.chat
    chat_title = chat.title or "этот чат"
    name = LANG_NAMES.get(lang, lang)

    # Banned words active in this chat (global + chat-specific), kept to the
    # chosen language by matching against that language's stem file.
    banned = {w.pattern for w in await get_banned_words(session, chat.id)}
    words = sorted(banned & _load_lang_words(lang))

    if words:
        header = f"🚫 Запрещённые слова ({name}) в чате «{chat_title}» — {len(words)} шт.:\n"
        messages = _split_messages(header, words)
    else:
        messages = [f"🚫 В чате «{chat_title}» нет активных запрещённых слов для {name}."]

    sent = await _send_dm(bot, callback.from_user.id, messages)
    if sent:
        await callback.answer("📩 Список отправлен вам в личные сообщения.")
        return

    # Couldn't DM the user (they haven't started the bot).
    if await is_user_admin(bot, chat.id, callback.from_user.id):
        me = await bot.get_me()
        await callback.message.answer(
            f"{mention(callback.from_user)}, не могу отправить вам список в личные сообщения. "
            f"Откройте диалог с ботом и нажмите «Start»: https://t.me/{me.username} — затем повторите."
        )
        await callback.answer("Не удалось написать в ЛС — смотрите сообщение в чате.", show_alert=True)
    else:
        # Regular user: never write to the chat, just dismiss the button quietly.
        await callback.answer()
