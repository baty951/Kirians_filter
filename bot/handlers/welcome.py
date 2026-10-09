import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import get_settings
from bot.database.crud import cancel_captcha, get_or_create_chat
from bot.database.models import ActionLog, CaptchaChallenge
from bot.utils.actions import MUTED_PERMISSIONS, UNMUTED_PERMISSIONS, safe_call
from bot.utils.captcha import build_captcha
from bot.utils.helpers import is_protected, mention, restriction_lock

router = Router(name="welcome")
logger = logging.getLogger(__name__)


def _deadline(challenge: CaptchaChallenge) -> datetime:
    return challenge.expires_at.replace(tzinfo=timezone.utc) if challenge.expires_at.tzinfo is None else challenge.expires_at


def _owns_restriction(member) -> bool:
    return member.status == "restricted" and member.until_date.timestamp() == 0 and all(
        getattr(member, field, None) is value
        for field, value in MUTED_PERMISSIONS.model_dump(exclude_none=True).items()
    )


async def _muted_by_admin(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    # action_logs are purged after ARCHIVE_RETENTION_DAYS, so older /mute records
    # are forgotten; store mutes separately if that turns out to matter.
    return await session.scalar(select(ActionLog.id).where(
        ActionLog.chat_id == chat_id, ActionLog.target_id == user_id,
        ActionLog.action.in_(("MUTE", "WARN_MUTE")),
    ).limit(1)) is not None


@router.message(F.new_chat_members)
async def on_join(message: Message, bot: Bot, session: AsyncSession) -> None:
    chat = await get_or_create_chat(session, message.chat.id, message.chat.title)
    for user in message.new_chat_members:
        if user.is_bot or is_protected(user.id, bot):
            continue
        if not chat.captcha_enabled or message.chat.type != ChatType.SUPERGROUP:
            if chat.welcome_enabled:
                text = chat.welcome_text or "👋 Добро пожаловать, {name}!"
                await message.answer(text.replace("{name}", mention(user)))
            continue
        async with restriction_lock(message.chat.id, user.id):
            member = await bot.get_chat_member(message.chat.id, user.id)
            if member.status in ("administrator", "creator"):
                continue
            challenge = await session.get(CaptchaChallenge, (message.chat.id, user.id))
            if member.status == "restricted" and not _owns_restriction(member):
                continue  # A restriction that this bot's captcha does not own.
            # Captcha mutes from before challenges were stored have no row; an admin's
            # permanent /mute looks the same, so only the mute log tells them apart.
            if member.status == "restricted" and challenge is None and await _muted_by_admin(session, message.chat.id, user.id):
                continue
            question, keyboard, answer = build_captcha(user.id)
            try:
                sent = await message.answer(f"{mention(user)}, {question}", reply_markup=keyboard)
            except Exception:
                logger.exception("Could not post captcha for %s in %s", user.id, message.chat.id)
                continue
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().captcha_timeout_seconds)
            if challenge is None:
                challenge = CaptchaChallenge(chat_id=message.chat.id, user_id=user.id)
                session.add(challenge)
            old_message_id = challenge.message_id
            challenge.message_id, challenge.answer = sent.message_id, answer
            challenge.expires_at, challenge.active = expires_at, True
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                await safe_call(bot.delete_message(message.chat.id, sent.message_id))
                logger.exception("Could not persist captcha; no new mute was applied")
                continue
            # Persist the question first, so a crash or send failure cannot orphan a mute.
            error = await safe_call(bot.restrict_chat_member(
                message.chat.id, user.id, permissions=MUTED_PERMISSIONS,
            ))
            if error:
                logger.warning("Captcha restriction failed for %s: %s", user.id, error)
            if old_message_id and old_message_id != sent.message_id:
                await safe_call(bot.delete_message(message.chat.id, old_message_id))


async def expire_captchas(bot: Bot, sessions: async_sessionmaker[AsyncSession]) -> None:
    """Resume expiry after restarts; failed challenges retain ownership of their mute."""
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        expired = list(await session.scalars(select(CaptchaChallenge).where(
            CaptchaChallenge.active.is_(True), CaptchaChallenge.expires_at <= now,
        )))
    for expired_challenge in expired:
        async with restriction_lock(expired_challenge.chat_id, expired_challenge.user_id):
            async with sessions() as session:
                challenge = await session.get(CaptchaChallenge, (expired_challenge.chat_id, expired_challenge.user_id))
                if (challenge is None or not challenge.active or _deadline(challenge) > now
                        or challenge.message_id != expired_challenge.message_id):
                    continue
                challenge.active = False
                await session.commit()
                await safe_call(bot.delete_message(challenge.chat_id, challenge.message_id))


@router.callback_query(F.data.startswith("captcha:"))
async def on_captcha_answer(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdecimal() or not parts[2].isdecimal() or not isinstance(callback.message, Message):
        await callback.answer("Некорректная проверка.", show_alert=True)
        return
    target_id, choice = int(parts[1]), int(parts[2])
    if callback.from_user.id != target_id:
        await callback.answer("Это не ваша капча.", show_alert=True)
        return
    chat_id = callback.message.chat.id
    async with restriction_lock(chat_id, target_id):
        challenge = await session.get(CaptchaChallenge, (chat_id, target_id))
        if (challenge is None or not challenge.active or _deadline(challenge) <= datetime.now(timezone.utc)
                or challenge.message_id != callback.message.message_id):
            await callback.answer("Эта проверка уже недействительна.", show_alert=True)
            return
        if choice != challenge.answer:
            challenge.active = False
            await session.commit()
            await safe_call(callback.message.delete())
            await callback.answer(
                "❌ Неверно. Вы останетесь без права писать. "
                "Выйдите из чата и зайдите снова, чтобы пройти проверку заново.",
                show_alert=True,
            )
            return
        try:
            member = await bot.get_chat_member(chat_id, target_id)
        except TelegramAPIError:
            await callback.answer("Не удалось проверить ограничение. Повторите попытку.", show_alert=True)
            return
        if member.status == "restricted" and not _owns_restriction(member):
            await cancel_captcha(session, chat_id, target_id)
            await callback.answer("Ограничение изменено администратором.", show_alert=True)
            return
        if member.status == "restricted":
            error = await safe_call(bot.restrict_chat_member(
                chat_id, target_id, permissions=UNMUTED_PERMISSIONS,
            ))
            if error:
                await callback.answer("Не удалось снять ограничение. Повторите попытку.", show_alert=True)
                return
        await cancel_captcha(session, chat_id, target_id)
        await safe_call(callback.message.delete())
        await callback.answer("✅ Добро пожаловать!")


@router.chat_member()
async def on_external_restriction(event: ChatMemberUpdated, bot: Bot, session: AsyncSession) -> None:
    old = event.old_chat_member
    was_member = old.status in ("member", "administrator", "creator") or (
        old.status == "restricted" and old.is_member
    )
    new = event.new_chat_member
    left = new.status == "left" or (new.status == "restricted" and not new.is_member)
    if event.from_user.id != bot.id and was_member and not left:
        async with restriction_lock(event.chat.id, event.new_chat_member.user.id):
            await cancel_captcha(session, event.chat.id, event.new_chat_member.user.id)
