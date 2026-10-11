from redis.exceptions import RedisError
from sqlalchemy import select

from bot.database.crud import get_or_create_chat
from bot.database.models import Warning
from bot.handlers.moderation import auto_moderate
from bot.middlewares.antiflood import AntiFloodMiddleware
from support import ADMIN, CHAT_ID, USER, DatabaseCase, message

TEXT = "Buy my cheap watches now"


class FakeRedis:
    def __init__(self):
        self.keys, self.calls, self.unavailable = {}, [], False

    async def set(self, key, value, nx=False, ex=None):
        self.calls.append((key, ex))
        if self.unavailable:
            raise RedisError("down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def eval(self, *args):
        return 0


class DuplicateTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.redis = FakeRedis()
        self.middleware = AntiFloodMiddleware(self.redis)

    async def flags(self, session, text=TEXT, user=USER, **extra):
        seen = {}

        async def handler(event, data):
            seen.update(data)

        await self.middleware(handler, message(text, user=user, **extra), {"session": session})
        return seen

    async def enable(self, session, **settings):
        chat = await get_or_create_chat(session, CHAT_ID)
        chat.filter_duplicates = True
        for field, value in settings.items():
            setattr(chat, field, value)
        await session.commit()

    async def test_second_identical_text_is_flagged_per_sender(self):
        async with self.sessions() as session:
            await self.enable(session)
            self.assertFalse((await self.flags(session))["duplicate"])
            self.assertTrue((await self.flags(session))["duplicate"])
            self.assertFalse((await self.flags(session, user=ADMIN))["duplicate"])  # another sender
            self.assertFalse((await self.flags(session, "Something else entirely"))["duplicate"])

    async def test_comparison_is_normalized_and_covers_captions(self):
        async with self.sessions() as session:
            await self.enable(session)
            await self.flags(session)
            self.assertTrue((await self.flags(session, TEXT.upper().replace("W", "W​")))["duplicate"])
            self.assertTrue((await self.flags(session, None, caption=TEXT))["duplicate"])

    async def test_short_texts_are_never_duplicates(self):
        async with self.sessions() as session:
            await self.enable(session)
            for _ in range(3):
                self.assertFalse((await self.flags(session, "ok, ok"))["duplicate"])
            self.assertFalse(self.redis.calls)

    async def test_window_is_the_chat_setting(self):
        async with self.sessions() as session:
            await self.enable(session, duplicate_window_seconds=60)
            await self.flags(session)
            self.assertEqual(self.redis.calls[-1][1], 60)

    async def test_disabled_filter_does_not_touch_redis(self):
        async with self.sessions() as session:
            self.assertNotIn("duplicate", await self.flags(session))
            self.assertFalse(self.redis.calls)

    async def test_redis_failure_degrades_to_not_duplicate(self):
        async with self.sessions() as session:
            await self.enable(session)
            self.redis.unavailable = True
            with self.assertLogs("bot.middlewares.antiflood", level="ERROR"):
                self.assertFalse((await self.flags(session))["duplicate"])

    async def test_auto_moderate_deletes_duplicates_and_can_warn(self):
        async with self.sessions() as session:
            self.assertFalse(await auto_moderate(message(TEXT).as_(self.bot), self.bot, session, duplicate=True))
            await self.enable(session)
            self.assertTrue(await auto_moderate(message(TEXT).as_(self.bot), self.bot, session, duplicate=True))
            self.assertFalse(await auto_moderate(message(TEXT).as_(self.bot), self.bot, session))
            self.assertFalse(list(await session.scalars(select(Warning))))
            (await get_or_create_chat(session, CHAT_ID)).warn_on_duplicates = True
            await session.commit()
            self.assertTrue(await auto_moderate(message(TEXT).as_(self.bot), self.bot, session, duplicate=True))
            self.assertEqual(len(list(await session.scalars(select(Warning)))), 1)
