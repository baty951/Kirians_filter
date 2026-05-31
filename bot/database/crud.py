from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from bot.database.models import ActionLog, BannedWord, Chat, StoredMessage, Warning


async def get_or_create_chat(session: AsyncSession, chat_id: int, title: str | None = None) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        settings = get_settings()
        chat = Chat(
            chat_id=chat_id,
            title=title,
            warn_limit=settings.default_warn_limit,
            mute_minutes=settings.default_mute_minutes,
        )
        session.add(chat)
        await session.commit()
    elif title and chat.title != title:
        chat.title = title
        await session.commit()
    return chat


async def add_warning(
    session: AsyncSession, chat_id: int, user_id: int, admin_id: int | None, reason: str | None
) -> int:
    """Add a warning and return the user's current warning count in this chat."""
    session.add(Warning(chat_id=chat_id, user_id=user_id, admin_id=admin_id, reason=reason))
    await session.commit()
    return await count_warnings(session, chat_id, user_id)


async def count_warnings(session: AsyncSession, chat_id: int, user_id: int) -> int:
    result = await session.scalar(
        select(func.count(Warning.id)).where(Warning.chat_id == chat_id, Warning.user_id == user_id)
    )
    return result or 0


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
    """Store an incoming message. Idempotent: re-seen ids are ignored."""
    stmt = (
        pg_insert(StoredMessage)
        .values(chat_id=chat_id, message_id=message_id, user_id=user_id, text=text, date=date)
        .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
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
    session.add(BannedWord(chat_id=chat_id, pattern=pattern, is_regex=is_regex))
    await session.commit()


async def remove_banned_word(session: AsyncSession, chat_id: int | None, pattern: str) -> int:
    result = await session.execute(
        delete(BannedWord).where(BannedWord.chat_id == chat_id, BannedWord.pattern == pattern)
    )
    await session.commit()
    return result.rowcount or 0


async def update_chat_setting(session: AsyncSession, chat_id: int, field: str, value) -> None:
    chat = await get_or_create_chat(session, chat_id)
    setattr(chat, field, value)
    await session.commit()
