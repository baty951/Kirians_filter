from collections.abc import Awaitable, Callable
from typing import Any
import logging

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import add_message

logger = logging.getLogger(__name__)


class MessageStoreMiddleware(BaseMiddleware):
    """Archive group/channel messages and edits using the per-update session."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL)
        ):
            session: AsyncSession | None = data.get("session")
            if session is not None:
                try:
                    await add_message(
                        session,
                        event.chat.id,
                        event.message_id,
                        event.sender_chat.id if event.sender_chat else (event.from_user.id if event.from_user else None),
                        event.text or event.caption,
                        event.date,
                    )
                except Exception:
                    # Never block message handling on a storage hiccup; keep the
                    # session usable for downstream handlers.
                    await session.rollback()
                    logger.exception("Could not store message %s in %s", event.message_id, event.chat.id)
        return await handler(event, data)
