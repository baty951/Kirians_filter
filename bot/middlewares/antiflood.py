import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message
from redis.asyncio import Redis
from redis.exceptions import RedisError

from bot.database.crud import get_or_create_chat
from bot.filters.content import fold

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


# Short replies ("ok", "+") are legitimately repeated.
_MIN_DUPLICATE_LENGTH = 8


class AntiFloodMiddleware(BaseMiddleware):
    """Atomically flag floods in a bounded sliding window; degrade on Redis errors."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def _is_duplicate(self, event: Message, sender_id: int, window: int) -> bool:
        """True if this sender already posted the same (normalized) text within `window` seconds."""
        text = fold(event.text or event.caption or "")
        if len(text) < _MIN_DUPLICATE_LENGTH:
            return False
        key = f"dup:{event.chat.id}:{sender_id}:{hashlib.sha1(text.encode()).hexdigest()[:16]}"
        try:
            # SET NX is atomic: the first copy claims the key, later copies within the window see it taken.
            return await self.redis.set(key, 1, nx=True, ex=window) is None
        except RedisError:
            logger.exception("Duplicate filter unavailable; continuing content checks")
            return False

    async def __call__(
        self, handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message, data: dict[str, Any],
    ) -> Any:
        if (event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
                or event.new_chat_members or event.left_chat_member or event.is_automatic_forward):
            return await handler(event, data)
        chat = await get_or_create_chat(data["session"], event.chat.id, event.chat.title)
        sender = event.sender_chat or event.from_user
        if sender is None:
            return await handler(event, data)
        if chat.filter_duplicates:
            data["duplicate"] = await self._is_duplicate(event, sender.id, chat.duplicate_window_seconds)
        if not chat.antiflood_enabled:
            return await handler(event, data)
        try:
            count = await self.redis.eval(
                _SLIDING_WINDOW, 1, f"flood:v2:{event.chat.id}:{sender.id}",
                chat.antiflood_window_seconds * 1000, chat.antiflood_messages, str(event.message_id),
            )
        except RedisError:
            logger.exception("Anti-flood unavailable; continuing content checks")
            count = 0
        data["flood_count"] = count
        data["flooding"] = count > chat.antiflood_messages
        return await handler(event, data)
