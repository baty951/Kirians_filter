from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message
from redis.asyncio import Redis

from config import get_settings


class AntiFloodMiddleware(BaseMiddleware):
    """Sliding-window rate limiter backed by Redis.

    Counts messages per (chat_id, user_id) within a window. When the count
    exceeds the threshold, the update is dropped and `flooding=True` is passed
    to the handler so a flood handler can mute/delete.
    """

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        settings = get_settings()
        self.limit = settings.antiflood_messages
        self.window = settings.antiflood_window_seconds

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        key = f"flood:{event.chat.id}:{event.from_user.id}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, self.window)

        data["flood_count"] = count
        data["flooding"] = count > self.limit
        return await handler(event, data)
