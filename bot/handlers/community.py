from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import list_approved, set_approved
from bot.handlers.admin import _guard
from bot.middlewares.admin_check import is_user_admin
from bot.utils.actions import ban_user, humanize_error, log_action, safe_call, warn_user
from bot.utils.helpers import extract_target_and_reason, is_protected, mention, restriction_lock

router = Router(name="community")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

REPORT_COOLDOWN_SECONDS = 60
MAX_PINGED_ADMINS = 5
# Reports already acted on, so two admins pressing at once cannot warn/ban twice.
# ponytail: per-process set, single polling instance; store in Redis if scaled out
_handled_reports: set[tuple[int, int]] = set()


@router.message(Command("approve"))
async def cmd_approve(message: Message, bot: Bot, session: AsyncSession) -> None:
    await _set_approved(message, bot, session, True)


@router.message(Command("unapprove"))
async def cmd_unapprove(message: Message, bot: Bot, session: AsyncSession) -> None:
    await _set_approved(message, bot, session, False)


async def _set_approved(message: Message, bot: Bot, session: AsyncSession, approved: bool) -> None:
    if not await _guard(message, bot):
        return
    target_id, _ = extract_target_and_reason(message)
    if target_id is None:
        await message.reply("Ответьте на сообщение или укажите ID пользователя.")
        return
    actor_id = message.from_user.id if message.from_user else None
    if not await set_approved(session, message.chat.id, target_id, actor_id, approved):
        await message.reply("Уже в этом состоянии." if approved else "Пользователь не был доверенным.")
        return
    word = "APPROVE" if approved else "UNAPPROVE"
    await message.reply("✅ Пользователь доверенный: автомодерация его не трогает." if approved
                        else "Доверие снято: автомодерация снова действует.")
    await log_action(bot, session, message.chat.id, f"{word}: user {target_id}",
                     action=word, actor_id=actor_id, target_id=target_id)


@router.message(Command("approved"))
async def cmd_approved(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not await _guard(message, bot):
        return
    rows = await list_approved(session, message.chat.id)
    if not rows:
        await message.reply("Доверенных пользователей нет.")
        return
    lines = [f'• <a href="tg://user?id={row.user_id}">{row.user_id}</a>' for row in rows]
    await message.reply("Доверенные (последние 50):\n" + "\n".join(lines))


def _report_keyboard(message_id: int, user_id: int) -> InlineKeyboardMarkup:
    def button(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"rpt:{action}:{message_id}:{user_id}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🗑 Удалить", "d"), button("⚠️ Варн", "w")],
        [button("🔨 Бан", "b"), button("✖ Игнор", "i")],
    ])


@router.message(Command("report"))
async def cmd_report(message: Message, bot: Bot, redis: Redis) -> None:
    reported = message.reply_to_message
    reporter = message.from_user
    if reported is None or reported.from_user is None or reporter is None:
        await message.reply("Ответьте командой /report на сообщение, на которое хотите пожаловаться.")
        return
    target = reported.from_user
    if (target.is_bot or target.id == reporter.id or is_protected(target.id, bot)
            or await is_user_admin(bot, message.chat.id, target.id)):
        await message.reply("На это сообщение пожаловаться нельзя.")
        return
    try:
        first = await redis.set(
            f"report:{message.chat.id}:{reporter.id}", 1, nx=True, ex=REPORT_COOLDOWN_SECONDS,
        ) is not None
    except RedisError:
        first = True  # an unavailable cooldown must not block reports
    if not first:
        await message.reply("Подождите минуту перед следующей жалобой.")
        return
    try:
        admins = [a.user for a in await bot.get_chat_administrators(message.chat.id) if not a.user.is_bot]
    except TelegramAPIError:
        admins = []
    reason = " ".join((message.text or "").split()[1:])[:200]
    text = f"🚩 Жалоба от {mention(reporter)} на {mention(target)}"
    if reason:
        text += f"\nПричина: {escape(reason)}"
    if admins:
        text += "\n" + " ".join(mention(a) for a in admins[:MAX_PINGED_ADMINS])
    await message.answer(text, reply_markup=_report_keyboard(reported.message_id, target.id))
    await safe_call(message.delete())


@router.callback_query(F.data.startswith("rpt:"))
async def on_report_action(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    parts = (callback.data or "").split(":")
    if (len(parts) != 4 or parts[1] not in ("d", "w", "b", "i") or not parts[2].isdecimal()
            or not parts[3].isdecimal() or not isinstance(callback.message, Message)):
        await callback.answer("Некорректная жалоба.", show_alert=True)
        return
    chat_id, admin = callback.message.chat.id, callback.from_user
    if not await is_user_admin(bot, chat_id, admin.id):
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    action, message_id, user_id = parts[1], int(parts[2]), int(parts[3])
    if action in ("w", "b") and (is_protected(user_id, bot) or await is_user_admin(bot, chat_id, user_id)):
        await callback.answer("Этот пользователь защищён от ограничений.", show_alert=True)
        return
    key = (chat_id, callback.message.message_id)
    async with restriction_lock(chat_id, user_id, "report"):
        if key in _handled_reports:
            await callback.answer("Жалоба уже обработана.", show_alert=True)
            return
        if len(_handled_reports) > 2000:
            _handled_reports.clear()
        _handled_reports.add(key)
        result = await _apply_report_action(bot, session, chat_id, admin.id, action, message_id, user_id)
    await safe_call(callback.message.edit_text(
        f"{callback.message.html_text}\n\n{mention(admin)}: {result}", reply_markup=None,
    ))
    await callback.answer()


async def _apply_report_action(
    bot: Bot, session: AsyncSession, chat_id: int, admin_id: int, action: str, message_id: int, user_id: int,
) -> str:
    if action == "i":
        return "жалоба отклонена"
    error = await safe_call(bot.delete_message(chat_id, message_id))
    await log_action(
        bot, session, chat_id, f"{'DELETE_FAILED' if error else 'DELETE'}: user {user_id}, жалоба",
        action="DELETE_FAILED" if error else "DELETE", actor_id=admin_id, target_id=user_id, reason="жалоба",
    )
    deleted = f"сообщение не удалено ({humanize_error(error)})" if error else "сообщение удалено"
    if action == "d":
        return deleted
    if action == "w":
        outcome, total, error = await warn_user(
            bot, session, chat_id, user_id, actor_id=admin_id, reason="жалоба",
        )
        if error:
            return f"{deleted}; наказать не удалось ({humanize_error(error)})"
        return deleted + {"WARN": f"; предупреждение {total}", "WARN_MUTE": "; заглушен",
                          "WARN_BAN": "; забанен"}[outcome]
    error = await ban_user(bot, chat_id, user_id, session=session)
    await log_action(
        bot, session, chat_id, f"{'BAN_FAILED' if error else 'BAN'}: user {user_id}, жалоба",
        action="BAN_FAILED" if error else "BAN", actor_id=admin_id, target_id=user_id, reason="жалоба",
    )
    return f"{deleted}; " + (f"бан не удался ({humanize_error(error)})" if error else "забанен")
