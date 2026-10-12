import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

os.environ.setdefault("BOT_TOKEN", "123456:ABC-test-token")

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message, MessageEntity, User
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.crud import invalidate_banned_words
from bot.database.models import Base
from bot.utils.actions import MUTED_PERMISSIONS
from bot.utils.captcha import EMOJI_NAMES
from redis.exceptions import RedisError

CHAT_ID = -100123
USER = User(id=42, is_bot=False, first_name="User")
ADMIN = User(id=99, is_bot=False, first_name="Admin")



def solve_captcha(text, keyboard):
    """Independently solve a captcha question; returns the callback value of the one correct button."""
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    found = re.search(r"(\d+) ([+\-×]) (\d+)", text)
    if found:
        left, op, right = int(found[1]), found[2], int(found[3])
        label = str({"+": left + right, "-": left - right, "×": left * right}[op])
    else:
        label = next(emoji for emoji, name in EMOJI_NAMES.items() if f"Нажмите на {name}." in text)
    correct = [button for button in buttons if button.text == label]
    assert len(correct) == 1, (text, [button.text for button in buttons])
    return int(correct[0].callback_data.rsplit(":", 1)[1])


class FakeRedis:
    """Redis double: SET NX with recorded calls, optional outage, no-op eval."""

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


def message(text="hello", *, message_id=1, chat_type="supergroup", user=USER, **extra):
    if text and text.startswith("/") and "entities" not in extra:
        extra["entities"] = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
    return Message(
        message_id=message_id, date=datetime.now(timezone.utc),
        chat=Chat(id=CHAT_ID, type=chat_type, title="Test <chat>"),
        from_user=user, text=text, **extra,
    )


def restricted_member(user=USER, **extra):
    values = MUTED_PERMISSIONS.model_dump(exclude_none=True)
    return SimpleNamespace(**({"status": "restricted", "user": user, "is_member": True,
                               "until_date": datetime.fromtimestamp(0, timezone.utc)} | values | extra))


class TelegramSession(BaseSession):
    """Telegram transport double: records requests and models membership changes."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.members = {}
        self.failures = defaultdict(list)
        self.hooks = {}
        self.next_message_id = 1000

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        self.calls.append(method)
        if name in self.hooks:
            await self.hooks[name](method)
        if self.failures[name]:
            raise TelegramBadRequest(method=method, message=self.failures[name].pop(0))
        if name == "getMe":
            return User(id=bot.id, is_bot=True, first_name="Bot", username="test_bot")
        if name == "getChatMember":
            return self.members.get(method.user_id, SimpleNamespace(status="member", user=USER))
        if name == "getChatAdministrators":
            return [SimpleNamespace(user=ADMIN, status="administrator")]
        if name == "restrictChatMember":
            if method.permissions.can_send_messages:
                self.members[method.user_id] = SimpleNamespace(status="member", user=USER)
            else:
                self.members[method.user_id] = restricted_member(until_date=method.until_date or datetime.fromtimestamp(0, timezone.utc))
        if name == "banChatMember":
            self.members[method.user_id] = SimpleNamespace(status="kicked", user=USER)
        if name == "unbanChatMember":
            self.members[method.user_id] = SimpleNamespace(status="left", user=USER)
        if name in ("sendMessage", "editMessageReplyMarkup"):
            self.next_message_id += 1
            return message(method.text if name == "sendMessage" else "menu",
                           message_id=self.next_message_id).as_(bot)
        return True

    def requests(self, name):
        return [call for call in self.calls if call.__api_method__ == name]


class DatabaseCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        invalidate_banned_words(None)  # tests share CHAT_ID; the cache is process-wide
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enforce_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.transport = TelegramSession()
        self.bot = Bot("123456:ABC-test-token", session=self.transport,
                       default=DefaultBotProperties(parse_mode="HTML"))
        self.transport.members[ADMIN.id] = SimpleNamespace(status="administrator")

    async def asyncTearDown(self):
        await self.bot.session.close()
        await self.engine.dispose()
