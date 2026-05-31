from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.crud import add_message


class MessageStoreMiddleware(BaseMiddleware):
    """Persist every incoming group message to the `messages` table.

    Reuses the per-update session injected by DbSessionMiddleware. /purge then
    references deleted message ids back to their stored author/time/text.
    """

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.from_user is not None
            and event.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        ):
            session: AsyncSession | None = data.get("session")
            if session is not None:
                try:
                    await add_message(
                        session,
                        event.chat.id,
                        event.message_id,
                        event.from_user.id,
                        event.text or event.caption,
                        event.date,
                    )
                except Exception:
                    # Never block message handling on a storage hiccup; keep the
                    # session usable for downstream handlers.
                    await session.rollback()
        return await handler(event, data)
