import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message
from redis.asyncio import Redis
from redis.exceptions import RedisError

from config import get_settings
from bot.database.crud import get_or_create_chat

logger = logging.getLogger(__name__)

_SLIDING_WINDOW = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
redis.call('ZADD', KEYS[1], now, ARGV[3])
local count = redis.call('ZCARD', KEYS[1])
if count > limit then
    redis.call('ZREMRANGEBYRANK', KEYS[1], 0, count - limit - 1)
end
redis.call('PEXPIRE', KEYS[1], window)
return count
"""


class AntiFloodMiddleware(BaseMiddleware):
    """Atomically flag floods in a bounded sliding window; degrade on Redis errors."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        settings = get_settings()
        self.limit = settings.antiflood_messages
        self.window = settings.antiflood_window_seconds

    async def __call__(
        self, handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message, data: dict[str, Any],
    ) -> Any:
        if (event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
                or event.new_chat_members or event.left_chat_member or event.is_automatic_forward):
            return await handler(event, data)
        chat = await get_or_create_chat(data["session"], event.chat.id, event.chat.title)
        if not chat.antiflood_enabled:
            return await handler(event, data)
        sender = event.sender_chat or event.from_user
        if sender is None:
            return await handler(event, data)
        try:
            count = await self.redis.eval(
                _SLIDING_WINDOW, 1, f"flood:v2:{event.chat.id}:{sender.id}",
                self.window * 1000, self.limit, str(event.message_id),
            )
        except RedisError:
            logger.exception("Anti-flood unavailable; continuing content checks")
            count = 0
        data["flood_count"] = count
        data["flooding"] = count > self.limit
        return await handler(event, data)
