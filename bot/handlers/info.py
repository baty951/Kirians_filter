from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import action_counts, get_or_create_chat, top_offenders, update_chat_setting, user_history
from bot.handlers.admin import _guard
from bot.utils.helpers import extract_target_and_reason

router = Router(name="info")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

DEFAULT_STATS_DAYS = 7
MAX_STATS_DAYS = 90
MAX_RULES_LENGTH = 1500
LABELS = {
    "DELETE": "Удалено сообщений", "WARN": "Предупреждений", "WARN_MUTE": "Мутов за варны",
    "WARN_BAN": "Банов за варны", "FLOOD_MUTE": "Мутов за флуд", "MUTE": "Ручных мутов", "BAN": "Банов",
    "KICK": "Киков", "UNMUTE": "Снято мутов", "UNBAN": "Снято банов", "PURGE": "Очисток",
}


@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    args = (message.text or "").split()[1:]
    days = DEFAULT_STATS_DAYS
    if args:
        if not args[0].isdecimal() or not 1 <= int(args[0]) <= MAX_STATS_DAYS:
            await message.reply(f"Использование: /stats [дней, 1–{MAX_STATS_DAYS}]")
            return
        days = int(args[0])
    since = datetime.now(timezone.utc) - timedelta(days=days)
    counts = await action_counts(session, message.chat.id, since)
    if not counts:
        await message.reply(f"За {days} дн. событий модерации нет.")
        return
    lines = [f"📊 Модерация за {days} дн.:"]
    lines += [f"• {LABELS.get(action, escape(action))}: {count}" for action, count in counts]
    offenders = await top_offenders(session, message.chat.id, since)
    if offenders:
        lines.append("\nЧаще всего нарушали:")
        lines += [f'• <a href="tg://user?id={user_id}">{user_id}</a> — {count}' for user_id, count in offenders]
    await message.reply("\n".join(lines))


@router.message(Command("modlog"))
async def cmd_modlog(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    rows = await user_history(session, message.chat.id, target_id)
    if not rows:
        await message.reply("По этому пользователю записей нет.")
        return
    lines = [
        f"{row.created_at:%d.%m %H:%M} {escape(row.action)}" + (f" — {escape(row.reason)}" if row.reason else "")
        for row in rows
    ]
    await message.reply(f"🗂 Последние действия по пользователю {target_id}:\n" + "\n".join(lines))


@router.message(Command("setrules"))
async def cmd_setrules(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    text = escape(parts[1].strip(), quote=False) if len(parts) > 1 and parts[1].strip() else None
    if text is not None and len(text) > MAX_RULES_LENGTH:
        await message.reply(f"Правила слишком длинные: не больше {MAX_RULES_LENGTH} символов.")
        return
    await get_or_create_chat(session, message.chat.id, message.chat.title)
    await update_chat_setting(session, message.chat.id, "rules_text", text)
    await message.reply("✅ Правила обновлены." if text else "Правила сброшены.")


@router.message(Command("rules"))
async def cmd_rules(message: Message, session: AsyncSession) -> None:
    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    await message.reply(f"📜 Правила чата:\n{chat.rules_text}" if chat.rules_text else "Правила не заданы.")
