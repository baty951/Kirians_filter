import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from bot.database.models import (
    ActionLog, ApprovedUser, BannedWord, CaptchaChallenge, Chat, MemberJoin, StoredMessage, Warning,
)


async def get_or_create_chat(session: AsyncSession, chat_id: int, title: str | None = None) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        settings = get_settings()
        stmt = pg_insert(Chat).values(
            chat_id=chat_id,
            title=title,
            warn_limit=settings.default_warn_limit,
            mute_minutes=settings.default_mute_minutes,
            warn_ttl_days=settings.default_warn_ttl_days,
            ban_after_mutes=settings.default_ban_after_mutes,
            flood_mute_minutes=settings.default_flood_mute_minutes,
            antiflood_messages=settings.antiflood_messages,
            antiflood_window_seconds=settings.antiflood_window_seconds,
        ).on_conflict_do_nothing(index_elements=["chat_id"])
        await session.execute(stmt)
        await session.commit()
        chat = await session.get(Chat, chat_id)
    elif title and chat.title != title:
        chat.title = title
        await session.commit()
    return chat


async def add_warning(
    session: AsyncSession, chat_id: int, user_id: int, admin_id: int | None, reason: str | None
) -> int:
    """Add a warning and return the user's current warning count in this chat."""
    validate_reason(reason)
    await get_or_create_chat(session, chat_id)
    session.add(Warning(chat_id=chat_id, user_id=user_id, admin_id=admin_id, reason=reason))
    await session.commit()
    return await count_warnings(session, chat_id, user_id)


async def count_warnings(session: AsyncSession, chat_id: int, user_id: int) -> int:
    """Count live warnings only: older than the chat's TTL they are ignored."""
    chat = await get_or_create_chat(session, chat_id)
    since = datetime.now(timezone.utc) - timedelta(days=chat.warn_ttl_days)
    result = await session.scalar(
        select(func.count(Warning.id)).where(
            Warning.chat_id == chat_id, Warning.user_id == user_id, Warning.created_at >= since,
        )
    )
    return result or 0


async def count_recent_actions(
    session: AsyncSession, chat_id: int, target_id: int, actions: tuple[str, ...],
    since: datetime, actor_id: int | None = None,
) -> int:
    stmt = select(func.count(ActionLog.id)).where(
        ActionLog.chat_id == chat_id, ActionLog.target_id == target_id,
        ActionLog.action.in_(actions), ActionLog.created_at >= since,
    )
    if actor_id is not None:
        stmt = stmt.where(ActionLog.actor_id == actor_id)
    return await session.scalar(stmt) or 0


async def reset_warnings(session: AsyncSession, chat_id: int, user_id: int) -> None:
    await session.execute(
        delete(Warning).where(Warning.chat_id == chat_id, Warning.user_id == user_id)
    )
    await session.commit()


async def add_message(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    user_id: int | None,
    text: str | None,
    date: datetime | None,
) -> None:
    """Keep the latest text when a message is edited or delivered again."""
    stmt = (
        pg_insert(StoredMessage)
        .values(chat_id=chat_id, message_id=message_id, user_id=user_id, text=text, date=date)
        .on_conflict_do_update(
            index_elements=["chat_id", "message_id"],
            set_={"user_id": user_id, "text": text, "date": date},
        )
    )
    await session.execute(stmt)
    await session.commit()


async def add_log(
    session: AsyncSession,
    chat_id: int,
    action: str,
    *,
    actor_id: int | None = None,
    target_id: int | None = None,
    reason: str | None = None,
    content: str | None = None,
) -> None:
    """Persist a single audit-log entry."""
    validate_reason(reason)
    await get_or_create_chat(session, chat_id)
    session.add(
        ActionLog(
            chat_id=chat_id,
            action=action,
            actor_id=actor_id,
            target_id=target_id,
            reason=reason,
            content=content,
        )
    )
    await session.commit()


# ponytail: per-process cache, single polling instance; Redis pub/sub if scaled out
_WORDS_TTL = 60.0
# key: chat_id, or None for the global list (stored once, not per chat)
_words_cache: dict[int | None, tuple[float, tuple[tuple[str, bool], ...]]] = {}
_words_gen = 0


def invalidate_banned_words(chat_id: int | None) -> None:
    """Drop cached words for a chat; None (global words) clears everything."""
    global _words_gen
    _words_gen += 1
    if chat_id is None:
        _words_cache.clear()
    else:
        _words_cache.pop(chat_id, None)


async def _cached_words(session: AsyncSession, chat_id: int | None) -> tuple[tuple[str, bool], ...]:
    hit = _words_cache.get(chat_id)
    if hit and time.monotonic() - hit[0] < _WORDS_TTL:
        return hit[1]
    generation = _words_gen
    where = BannedWord.chat_id.is_(None) if chat_id is None else BannedWord.chat_id == chat_id
    rows = await session.scalars(select(BannedWord).where(where))
    words = tuple((w.pattern, w.is_regex) for w in rows)
    if generation == _words_gen:  # a writer invalidated meanwhile: don't store stale words
        _words_cache[chat_id] = (time.monotonic(), words)
    return words


async def get_banned_words_cached(session: AsyncSession, chat_id: int) -> tuple[tuple[str, bool], ...]:
    """(pattern, is_regex) pairs for a chat plus global words, cached for _WORDS_TTL seconds."""
    return await _cached_words(session, chat_id) + await _cached_words(session, None)


async def get_banned_words(session: AsyncSession, chat_id: int) -> list[BannedWord]:
    """Words specific to this chat plus global words (chat_id IS NULL)."""
    result = await session.scalars(
        select(BannedWord).where(
            (BannedWord.chat_id == chat_id) | (BannedWord.chat_id.is_(None))
        )
    )
    return list(result)


async def add_banned_word(
    session: AsyncSession, chat_id: int | None, pattern: str, is_regex: bool = False
) -> None:
    if not pattern.strip() or len(pattern) > 256:
        raise ValueError("Слово или фраза должны содержать от 1 до 256 символов.")
    if chat_id is not None:
        await get_or_create_chat(session, chat_id)
    session.add(BannedWord(chat_id=chat_id, pattern=pattern, is_regex=is_regex))
    await session.commit()
    invalidate_banned_words(chat_id)


async def remove_banned_word(session: AsyncSession, chat_id: int | None, pattern: str) -> int:
    result = await session.execute(
        delete(BannedWord).where(BannedWord.chat_id == chat_id, BannedWord.pattern == pattern)
    )
    await session.commit()
    invalidate_banned_words(chat_id)
    return result.rowcount or 0


async def update_chat_setting(session: AsyncSession, chat_id: int, field: str, value) -> None:
    chat = await get_or_create_chat(session, chat_id)
    setattr(chat, field, value)
    await session.commit()


def validate_reason(reason: str | None) -> None:
    if reason is not None and len(reason) > 512:
        raise ValueError("Причина должна содержать не более 512 символов.")


async def cancel_captcha(session: AsyncSession, chat_id: int, user_id: int) -> None:
    await session.execute(delete(CaptchaChallenge).where(
        CaptchaChallenge.chat_id == chat_id, CaptchaChallenge.user_id == user_id,
    ))
    await session.commit()


async def failed_captchas(session: AsyncSession, chat_id: int, limit: int = 30) -> list[CaptchaChallenge]:
    """Members who failed or missed the captcha and are still muted (active challenges are not listed)."""
    return list(await session.scalars(
        select(CaptchaChallenge)
        .where(CaptchaChallenge.chat_id == chat_id, CaptchaChallenge.active.is_(False))
        .order_by(CaptchaChallenge.expires_at.desc()).limit(limit)
    ))


async def purge_archives(session: AsyncSession, before: datetime) -> None:
    for model in (StoredMessage, ActionLog):
        await session.execute(delete(model).where(model.created_at < before))
    await session.execute(delete(MemberJoin).where(MemberJoin.joined_at < before))
    await session.commit()


async def record_join(session: AsyncSession, chat_id: int, user_id: int) -> None:
    """Remember (or reset, on rejoin) when the member joined."""
    now = datetime.now(timezone.utc)
    await session.execute(
        pg_insert(MemberJoin).values(chat_id=chat_id, user_id=user_id, joined_at=now)
        .on_conflict_do_update(index_elements=["chat_id", "user_id"], set_={"joined_at": now})
    )
    await session.commit()


async def is_newbie(session: AsyncSession, chat_id: int, user_id: int, hours: int) -> bool:
    """True if the member joined within `hours`; members with no recorded join are not newbies."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return await session.scalar(select(MemberJoin.user_id).where(
        MemberJoin.chat_id == chat_id, MemberJoin.user_id == user_id, MemberJoin.joined_at >= since,
    )) is not None


async def set_approved(session: AsyncSession, chat_id: int, user_id: int, approved_by: int | None, approved: bool) -> bool:
    """Approve or un-approve a member. Returns False if there was nothing to change."""
    row = await session.get(ApprovedUser, (chat_id, user_id))
    if approved == (row is not None):
        return False
    if approved:
        session.add(ApprovedUser(chat_id=chat_id, user_id=user_id, approved_by=approved_by))
    else:
        await session.delete(row)
    await session.commit()
    return True


async def is_approved(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    return await session.get(ApprovedUser, (chat_id, user_id)) is not None


async def list_approved(session: AsyncSession, chat_id: int, limit: int = 50) -> list[ApprovedUser]:
    return list(await session.scalars(
        select(ApprovedUser).where(ApprovedUser.chat_id == chat_id)
        .order_by(ApprovedUser.created_at.desc()).limit(limit)
    ))
