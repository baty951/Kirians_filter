import json
from datetime import timedelta
from html import escape

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
    validate_reason,
)
from bot.filters.language import SCRIPT_RANGES, normalize_scripts, parse_scripts
from bot.middlewares.admin_check import is_user_admin
from bot.utils.actions import (
    ban_user,
    humanize_error,
    kick_user,
    log_action,
    mute_user,
    safe_call,
    unban_user,
    unmute_user,
)
from bot.utils.helpers import extract_target_and_reason, is_protected, parse_duration

router = Router(name="admin")
# All handlers in this router only apply to group chats.
router.message.filter(lambda m: m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL))


async def _guard(message: Message, bot: Bot) -> bool:
    """Ensure the caller is an admin. Returns False (and warns) otherwise."""
    if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
        return True  # Telegram authenticates anonymous admins and channel publishers.
    if message.from_user is None or not await is_user_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Команда доступна только администраторам.")
        return False
    return True


async def _valid_reason(message: Message, reason: str | None) -> bool:
    try:
        validate_reason(reason)
    except ValueError as error:
        await message.reply(str(error))
        return False
    return True


async def _log_failure(message: Message, bot: Bot, session: AsyncSession, action: str, target_id: int, error: str) -> None:
    await log_action(
        bot, session, message.chat.id, f"{action}_FAILED: user {target_id}, ошибка: {error}",
        action=f"{action}_FAILED", actor_id=message.from_user.id if message.from_user else None,
        target_id=target_id, content=error,
    )


@router.channel_post(Command("warn"))
@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, reason = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    if is_protected(target_id, bot):
        await message.reply("Этот пользователь защищён от ограничений.")
        return

    if not await _valid_reason(message, reason):
        return
    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    total = await add_warning(session, message.chat.id, target_id, message.from_user.id if message.from_user else None, reason)

    if total >= chat.warn_limit:
        err = await mute_user(bot, message.chat.id, target_id, timedelta(minutes=chat.mute_minutes), session=session)
        if err:
            await _log_failure(message, bot, session, "WARN_MUTE", target_id, err)
            await message.reply(
                f"⚠️ Лимит {total}/{chat.warn_limit} достигнут, но заглушить не удалось: "
                f"{humanize_error(err)}."
            )
            return
        await reset_warnings(session, message.chat.id, target_id)
        await message.reply(
            f"🔇 Пользователь набрал {total}/{chat.warn_limit} предупреждений "
            f"и заглушен на {chat.mute_minutes} мин."
        )
        await log_action(
            bot, session, message.chat.id,
            f"WARN→MUTE: user {target_id}, причина: {reason or '—'}",
            action="WARN_MUTE", actor_id=message.from_user.id if message.from_user else None, target_id=target_id, reason=reason,
        )
    else:
        await message.reply(
            f"⚠️ Предупреждение {total}/{chat.warn_limit}." + (f"\nПричина: {escape(reason)}" if reason else "")
        )
        await log_action(
            bot, session, message.chat.id,
            f"WARN {total}: user {target_id}, причина: {reason or '—'}",
            action="WARN", actor_id=message.from_user.id if message.from_user else None, target_id=target_id, reason=reason,
        )


@router.channel_post(Command("unwarn"))
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


@router.channel_post(Command("warns"))
@router.message(Command("warns"))
async def cmd_warns(message: Message, bot: Bot, session: AsyncSession) -> None:
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    total = await count_warnings(session, message.chat.id, target_id)
    await message.reply(f"У пользователя {total} предупреждени(й).")


@router.channel_post(Command("mute"))
@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, rest = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    if is_protected(target_id, bot):
        await message.reply("Этот пользователь защищён от ограничений.")
        return
    # First token of the remainder may be a duration like 30m/2h/1d.
    duration = None
    reason = rest
    human = "навсегда"
    if rest:
        first, _, tail = rest.partition(" ")
        try:
            parsed = parse_duration(first)
        except ValueError as error:
            await message.reply(str(error))
            return
        if parsed is not None:
            duration, reason, human = parsed, tail or None, first
    if not await _valid_reason(message, reason):
        return
    err = await mute_user(bot, message.chat.id, target_id, duration, session=session)
    if err:
        await _log_failure(message, bot, session, "MUTE", target_id, err)
        await message.reply(f"⚠️ Не удалось заглушить пользователя: {humanize_error(err)}.")
        return
    await message.reply(f"🔇 Пользователь заглушен ({human}).")
    await log_action(
        bot, session, message.chat.id,
        f"MUTE: user {target_id}, срок: {human}, причина: {reason or '—'}",
        action="MUTE", actor_id=message.from_user.id if message.from_user else None, target_id=target_id, reason=reason, content=f"срок: {human}",
    )


@router.channel_post(Command("unmute"))
@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    err = await unmute_user(bot, message.chat.id, target_id, session=session)
    if err:
        await _log_failure(message, bot, session, "UNMUTE", target_id, err)
        await message.reply(f"⚠️ Не удалось снять заглушение: {humanize_error(err)}.")
        return
    await message.reply("🔊 Заглушение снято.")
    await log_action(
        bot, session, message.chat.id, f"UNMUTE: user {target_id}",
        action="UNMUTE", actor_id=message.from_user.id if message.from_user else None, target_id=target_id,
    )


@router.channel_post(Command("ban"))
@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, reason = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    if is_protected(target_id, bot):
        await message.reply("Этот пользователь защищён от ограничений.")
        return
    if not await _valid_reason(message, reason):
        return
    err = await ban_user(bot, message.chat.id, target_id, session=session)
    if err:
        await _log_failure(message, bot, session, "BAN", target_id, err)
        await message.reply(f"⚠️ Не удалось забанить пользователя: {humanize_error(err)}.")
        return
    await message.reply("🔨 Пользователь забанен." + (f"\nПричина: {escape(reason)}" if reason else ""))
    await log_action(
        bot, session, message.chat.id, f"BAN: user {target_id}, причина: {reason or '—'}",
        action="BAN", actor_id=message.from_user.id if message.from_user else None, target_id=target_id, reason=reason,
    )


@router.channel_post(Command("unban"))
@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Укажите ID пользователя.")
        return
    err = await unban_user(bot, message.chat.id, target_id, session=session)
    if err:
        await _log_failure(message, bot, session, "UNBAN", target_id, err)
        await message.reply(f"⚠️ Не удалось разбанить пользователя: {humanize_error(err)}.")
        return
    await message.reply("✅ Пользователь разбанен.")
    await log_action(
        bot, session, message.chat.id, f"UNBAN: user {target_id}",
        action="UNBAN", actor_id=message.from_user.id if message.from_user else None, target_id=target_id,
    )


@router.channel_post(Command("kick"))
@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    target_id, reason = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    if is_protected(target_id, bot):
        await message.reply("Этот пользователь защищён от ограничений.")
        return
    if not await _valid_reason(message, reason):
        return
    err = await kick_user(bot, message.chat.id, target_id, session=session)
    if err:
        await _log_failure(message, bot, session, "KICK", target_id, err)
        await message.reply(f"⚠️ Не удалось удалить пользователя: {humanize_error(err)}.")
        return
    await message.reply("👢 Пользователь удалён из чата.")
    await log_action(
        bot, session, message.chat.id, f"KICK: user {target_id}, причина: {reason or '—'}",
        action="KICK", actor_id=message.from_user.id if message.from_user else None, target_id=target_id, reason=reason,
    )


@router.channel_post(Command("purge"))
@router.message(Command("purge"))
async def cmd_purge(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение, начиная с которого удалить.")
        return
    start, end = message.reply_to_message.message_id, message.message_id
    if start > end:
        await message.reply("Некорректный диапазон сообщений.")
        return
    submitted = 0
    error = None
    for chunk_start in range(start, end + 1, 100):
        chunk = list(range(chunk_start, min(chunk_start + 100, end + 1)))
        error = await safe_call(bot.delete_messages(message.chat.id, chunk))
        if error:
            break
        submitted += len(chunk)
    action = "PURGE" if error is None else ("PURGE_PARTIAL" if submitted else "PURGE_FAILED")
    requested = end - start + 1
    await log_action(
        bot, session, message.chat.id,
        f"{action}: диапазон {start}–{end}, передано {submitted}/{requested} ID",
        action=action, actor_id=message.from_user.id if message.from_user else None,
        content=json.dumps({"range": [start, end], "requested": requested, "submitted": submitted, "error": error}, ensure_ascii=False),
    )
    if error:
        await message.reply(f"⚠️ Очистка завершилась частично: {humanize_error(error)}.")


@router.channel_post(Command("addword"))
@router.message(Command("addword"))
async def cmd_addword(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: /addword <слово или фраза>")
        return
    word = parts[1].strip()
    if not word or len(word) > 256:
        await message.reply("Слово или фраза должны содержать от 1 до 256 символов.")
        return
    await add_banned_word(session, message.chat.id, word)
    await message.reply(f"✅ Слово добавлено в фильтр: «{escape(word)}»")


@router.channel_post(Command("delword"))
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


@router.channel_post(Command("langs"))
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


@router.channel_post(Command("blocklang"))
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
        await message.reply(f"Неизвестные письменности: {escape(', '.join(unknown))}.\nДоступно: {', '.join(SCRIPT_RANGES)}")
        return

    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    merged = sorted(set(parse_scripts(chat.blocked_scripts)) | set(valid))
    await update_chat_setting(session, message.chat.id, "blocked_scripts", ",".join(merged))
    await update_chat_setting(session, message.chat.id, "filter_language", True)

    note = f"\n⚠️ Пропущено (неизвестно): {escape(', '.join(unknown))}" if unknown else ""
    await message.reply(f"🌐 Языковой фильтр включён. Блокируются: {', '.join(merged)}{note}")


@router.channel_post(Command("allowlang"))
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


@router.channel_post(Command("setlog"))
@router.message(Command("setlog"))
async def cmd_setlog(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    await update_chat_setting(session, message.chat.id, "log_channel_id", message.chat.id)
    await message.reply("📝 Этот чат назначен лог-каналом для действий модерации.")
