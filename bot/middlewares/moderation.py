from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message

from bot.handlers.moderation import auto_moderate


class ModerationMiddleware(BaseMiddleware):
    """Check content before routing, including messages containing commands."""

    async def __call__(
        self, handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message, data: dict[str, Any],
    ) -> Any:
        if await auto_moderate(event, data["bot"], data["session"], data.get("flooding", False)):
            return None
        return await handler(event, data)
