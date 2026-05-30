from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import (
    add_banned_word,
    add_warning,
    count_warnings,
    get_or_create_chat,
    remove_banned_word,
    reset_warnings,
    update_chat_setting,
)
from bot.filters.language import SCRIPT_RANGES, normalize_scripts, parse_scripts
from bot.middlewares.admin_check import is_user_admin
from bot.utils.actions import ban_user, kick_user, log_action, mute_user, unban_user, unmute_user
from bot.utils.helpers import extract_target_and_reason, parse_duration

router = Router(name="admin")
# All handlers in this router only apply to group chats.
router.message.filter(lambda m: m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


async def _guard(message: Message, bot: Bot) -> bool:
    """Ensure the caller is an admin. Returns False (and warns) otherwise."""
    if message.from_user is None or not await is_user_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Команда доступна только администраторам.")
        return False
    return True


@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, reason = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return

    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    total = await add_warning(session, message.chat.id, target_id, message.from_user.id, reason)

    if total >= chat.warn_limit:
        await mute_user(bot, message.chat.id, target_id, parse_duration(f"{chat.mute_minutes}m"))
        await reset_warnings(session, message.chat.id, target_id)
        await message.reply(
            f"🔇 Пользователь набрал {total}/{chat.warn_limit} предупреждений "
            f"и заглушен на {chat.mute_minutes} мин."
        )
        await log_action(bot, session, message.chat.id, f"WARN→MUTE: user {target_id}, причина: {reason or '—'}")
    else:
        await message.reply(
            f"⚠️ Предупреждение {total}/{chat.warn_limit}." + (f"\nПричина: {reason}" if reason else "")
        )
        await log_action(bot, session, message.chat.id, f"WARN {total}: user {target_id}, причина: {reason or '—'}")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    await reset_warnings(session, message.chat.id, target_id)
    await message.reply("✅ Предупреждения сброшены.")


@router.message(Command("warns"))
async def cmd_warns(message: Message, bot: Bot, session: AsyncSession) -> None:
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    total = await count_warnings(session, message.chat.id, target_id)
    await message.reply(f"У пользователя {total} предупреждени(й).")


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, rest = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    # First token of the remainder may be a duration like 30m/2h/1d.
    duration = None
    reason = rest
    human = "навсегда"
    if rest:
        first, _, tail = rest.partition(" ")
        parsed = parse_duration(first)
        if parsed:
            duration, reason, human = parsed, tail or None, first
    await mute_user(bot, message.chat.id, target_id, duration)
    await message.reply(f"🔇 Пользователь заглушен ({human}).")
    await log_action(bot, session, message.chat.id, f"MUTE: user {target_id}, срок: {human}, причина: {reason or '—'}")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    await unmute_user(bot, message.chat.id, target_id)
    await message.reply("🔊 Заглушение снято.")
    await log_action(bot, session, message.chat.id, f"UNMUTE: user {target_id}")


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, reason = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    await ban_user(bot, message.chat.id, target_id)
    await message.reply("🔨 Пользователь забанен." + (f"\nПричина: {reason}" if reason else ""))
    await log_action(bot, session, message.chat.id, f"BAN: user {target_id}, причина: {reason or '—'}")


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Укажите ID пользователя.")
        return
    await unban_user(bot, message.chat.id, target_id)
    await message.reply("✅ Пользователь разбанен.")
    await log_action(bot, session, message.chat.id, f"UNBAN: user {target_id}")


@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, reason = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    await kick_user(bot, message.chat.id, target_id)
    await message.reply("👢 Пользователь удалён из чата.")
    await log_action(bot, session, message.chat.id, f"KICK: user {target_id}, причина: {reason or '—'}")


@router.message(Command("purge"))
async def cmd_purge(message: Message, bot: Bot) -> None:
    if not await _guard(message, bot):
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение, начиная с которого удалить.")
        return
    # Delete the range [reply_to_message .. current command].
    ids = list(range(message.reply_to_message.message_id, message.message_id + 1))
    for chunk_start in range(0, len(ids), 100):
        chunk = ids[chunk_start : chunk_start + 100]
        try:
            await bot.delete_messages(message.chat.id, chunk)
        except Exception:
            pass


@router.message(Command("addword"))
async def cmd_addword(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /addword <слово или фраза>")
        return
    await add_banned_word(session, message.chat.id, parts[1].strip())
    await message.reply(f"✅ Слово добавлено в фильтр: «{parts[1].strip()}»")


@router.message(Command("delword"))
async def cmd_delword(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /delword <слово или фраза>")
        return
    removed = await remove_banned_word(session, message.chat.id, parts[1].strip())
    await message.reply("🗑 Удалено." if removed else "Слово не найдено в фильтре этого чата.")


@router.message(Command("langs"))
async def cmd_langs(message: Message, bot: Bot, session: AsyncSession) -> None:
    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    current = parse_scripts(chat.blocked_scripts)
    state = "вкл" if chat.filter_language else "выкл"
    await message.reply(
        f"🌐 Языковой фильтр: <b>{state}</b>\n"
        f"Блокируются: {', '.join(current) or '—'}\n"
        f"Доступно: {', '.join(SCRIPT_RANGES)}\n\n"
        "Использование: /blocklang arabic chinese — добавить, /allowlang arabic — убрать."
    )


@router.message(Command("blocklang"))
async def cmd_blocklang(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    args = (message.text or "").split()[1:]
    if not args:
        await message.reply("Использование: /blocklang <письменность> [...]\nНапр.: /blocklang arabic chinese")
        return
    valid, unknown = normalize_scripts(args)
    if not valid:
        await message.reply(f"Неизвестные письменности: {', '.join(unknown)}.\nДоступно: {', '.join(SCRIPT_RANGES)}")
        return

    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    merged = sorted(set(parse_scripts(chat.blocked_scripts)) | set(valid))
    await update_chat_setting(session, message.chat.id, "blocked_scripts", ",".join(merged))
    await update_chat_setting(session, message.chat.id, "filter_language", True)

    note = f"\n⚠️ Пропущено (неизвестно): {', '.join(unknown)}" if unknown else ""
    await message.reply(f"🌐 Языковой фильтр включён. Блокируются: {', '.join(merged)}{note}")


@router.message(Command("allowlang"))
async def cmd_allowlang(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    args = (message.text or "").split()[1:]
    if not args:
        await message.reply("Использование: /allowlang <письменность> [...]")
        return
    valid, _ = normalize_scripts(args)
    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    remaining = sorted(set(parse_scripts(chat.blocked_scripts)) - set(valid))
    await update_chat_setting(session, message.chat.id, "blocked_scripts", ",".join(remaining) or None)
    await message.reply(f"🌐 Теперь блокируются: {', '.join(remaining) or '—'}")


@router.message(Command("setlog"))
async def cmd_setlog(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    await update_chat_setting(session, message.chat.id, "log_channel_id", message.chat.id)
    await message.reply("📝 Этот чат назначен лог-каналом для действий модерации.")
