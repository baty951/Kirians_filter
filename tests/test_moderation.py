import json
from datetime import datetime, timedelta, timezone

from aiogram.types import Chat, MessageEntity, Update
from redis.exceptions import ConnectionError
from sqlalchemy import select

from bot.database.crud import add_banned_word, get_or_create_chat, purge_archives
from bot.database.models import ActionLog, StoredMessage
from bot.handlers.admin import cmd_addword, cmd_mute, cmd_purge, cmd_warn
from bot.handlers.moderation import auto_moderate
from bot.filters.content import contains_link
from bot.utils.actions import log_action
from config import get_settings
from main import ALLOWED_UPDATES, create_dispatcher
from support import ADMIN, CHAT_ID, USER, DatabaseCase, message


class FloodCounter:
    def __init__(self):
        self.count = 0
        self.unavailable = False

    async def eval(self, *args):
        if self.unavailable:
            raise ConnectionError("Redis unavailable")
        self.count += 1
        return self.count


class DispatcherTests(DatabaseCase):
    @classmethod
    def setUpClass(cls):
        cls.counter = FloodCounter()
        # Router instances are attached once; each test supplies a fresh database.
        cls.dp = create_dispatcher(cls.counter, lambda: cls.current_sessions())

    async def asyncSetUp(self):
        await super().asyncSetUp()
        type(self).current_sessions = self.sessions
        self.counter.count, self.counter.unavailable = 0, False

    async def feed(self, event, update_type="message"):
        with self.assertNoLogs("kirians_filter", level="ERROR"):
            await self.dp.feed_update(self.bot, Update(update_id=event.message_id, **{update_type: event}))

    async def enable(self, **toggles):
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
            for field, value in toggles.items():
                setattr(chat, field, value)
            await session.commit()

    async def test_known_command_cannot_bypass_links_or_words(self):
        await self.enable(filter_links=True)
        async with self.sessions() as session:
            await add_banned_word(session, CHAT_ID, "forbidden")
        for index, text in enumerate(("/help https://spam.dev", "/help forbidden"), 1):
            await self.feed(message(text, message_id=index))
        self.assertEqual(len(self.transport.requests("deleteMessage")), 2)
        self.assertFalse(self.transport.requests("sendMessage"))

    async def test_known_commands_are_counted_before_routing(self):
        for index in range(1, 8):
            await self.feed(message("/help", message_id=index))
        self.assertEqual(len(self.transport.requests("sendMessage")), get_settings().antiflood_messages)
        self.assertEqual(len(self.transport.requests("deleteMessage")), 2)
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 2)

    async def test_join_leave_spam_is_not_flood(self):
        for index in range(1, 9):
            event = {"new_chat_members": [USER]} if index % 2 else {"left_chat_member": USER}
            await self.feed(message(None, message_id=index, **event))
        self.assertEqual(self.counter.count, 0)
        # Only replaced captcha questions (ids from the bot) may be deleted.
        self.assertFalse([r for r in self.transport.requests("deleteMessage") if r.message_id < 1000])

    async def test_edits_and_channel_posts_are_moderated_and_archived(self):
        await self.enable(filter_links=True)
        await self.feed(message("clean", message_id=10))
        await self.feed(message("https://spam.dev", message_id=10), "edited_message")
        for index, kind in enumerate(("channel_post", "edited_channel_post"), 11):
            await self.feed(message("https://spam.dev", message_id=index, chat_type="channel", user=None,
                                    sender_chat=Chat(id=CHAT_ID, type="channel", title="Publisher")), kind)
        self.assertEqual(len(self.transport.requests("deleteMessage")), 3)
        self.assertEqual(self.counter.count, 1)  # Edits are not new flood events.
        async with self.sessions() as session:
            stored = await session.get(StoredMessage, (CHAT_ID, 10))
            self.assertEqual(stored.text, "https://spam.dev")
            self.assertEqual(len(list(await session.scalars(select(StoredMessage)))), 3)
        self.assertTrue(set(("edited_message", "channel_post", "edited_channel_post", "chat_member")) <= set(ALLOWED_UPDATES))

    async def test_sender_chat_is_checked_despite_technical_bot_sender(self):
        await self.enable(filter_links=True)
        from aiogram.types import User
        fake = User(id=777000, is_bot=True, first_name="Telegram")
        await self.feed(message("https://spam.dev", user=fake, sender_chat=Chat(id=-100999, type="channel", title="Other")))
        self.assertEqual(len(self.transport.requests("deleteMessage")), 1)
        async with self.sessions() as session:
            self.assertEqual((await session.scalar(select(ActionLog))).target_id, -100999)

    async def test_channel_can_be_configured_without_from_user(self):
        await self.feed(message("/settings", chat_type="channel", user=None,
                                sender_chat=Chat(id=CHAT_ID, type="channel", title="Publisher")), "channel_post")
        self.assertEqual(len(self.transport.requests("sendMessage")), 1)

    async def test_admin_commands_and_anonymous_admin_are_exempt(self):
        await self.enable(filter_links=True)
        await self.feed(message("/help https://spam.dev", user=ADMIN))
        await self.feed(message("https://spam.dev", message_id=2,
                                sender_chat=Chat(id=CHAT_ID, type="supergroup", title="Group")))
        self.assertFalse(self.transport.requests("deleteMessage"))
        self.assertEqual(len(self.transport.requests("sendMessage")), 1)

    async def test_redis_outage_preserves_commands_and_content_checks(self):
        await self.enable(filter_links=True)
        self.counter.unavailable = True
        with self.assertLogs("bot.middlewares.antiflood", level="ERROR"):
            await self.feed(message("/help"))
            await self.feed(message("/help https://spam.dev", message_id=2))
        self.assertEqual(len(self.transport.requests("sendMessage")), 1)
        self.assertEqual(len(self.transport.requests("deleteMessage")), 1)

    async def test_disabled_antiflood_does_not_access_redis(self):
        await self.enable(antiflood_enabled=False)
        self.counter.unavailable = True
        with self.assertNoLogs("bot.middlewares.antiflood", level="ERROR"):
            await self.feed(message("/help"))
        self.assertEqual(self.counter.count, 0)
        self.assertEqual(len(self.transport.requests("sendMessage")), 1)


class ModerationTests(DatabaseCase):
    async def test_hidden_text_and_caption_links(self):
        hidden = MessageEntity(type="text_link", offset=0, length=4, url="https://example.org")
        self.assertTrue(contains_link("read", [hidden]))
        self.assertTrue(contains_link("example.dev", [MessageEntity(type="url", offset=0, length=11)]))
        self.assertFalse(contains_link("сделал.потом ok.thanks main.py"))
        self.assertFalse(contains_link("ordinary text"))
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
            chat.filter_links = True
            await session.commit()
            self.assertTrue(await auto_moderate(message("read", entities=[hidden]).as_(self.bot), self.bot, session))
            self.assertTrue(await auto_moderate(message(None, caption="read", caption_entities=[hidden]).as_(self.bot), self.bot, session))

    async def test_additional_media_types_are_blocked(self):
        payloads = {
            "document": {"file_id": "f", "file_unique_id": "u"},
            "audio": {"file_id": "f", "file_unique_id": "u", "duration": 1},
            "voice": {"file_id": "f", "file_unique_id": "u", "duration": 1},
            "video_note": {"file_id": "f", "file_unique_id": "u", "duration": 1, "length": 100},
            "paid_media": {"star_count": 1, "paid_media": [{"type": "preview"}]},
            "story": {"chat": {"id": CHAT_ID, "type": "supergroup"}, "id": 1},
        }
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
            chat.filter_media = True
            await session.commit()
            for field, payload in payloads.items():
                with self.subTest(field=field):
                    self.assertTrue(await auto_moderate(message(None, **{field: payload}).as_(self.bot), self.bot, session))

    async def test_failed_delete_and_flood_mute_are_recorded_as_failures(self):
        self.transport.failures["deleteMessage"].append("cannot delete")
        self.transport.failures["restrictChatMember"].append("not enough rights")
        async with self.sessions() as session:
            with self.assertLogs("bot.utils.actions", level="WARNING"):
                self.assertTrue(await auto_moderate(message().as_(self.bot), self.bot, session, flooding=True))
            logs = list(await session.scalars(select(ActionLog).order_by(ActionLog.id)))
            self.assertEqual([row.action for row in logs], ["DELETE_FAILED", "FLOOD_MUTE_FAILED"])

    async def test_first_mute_and_addword_create_parent_chat(self):
        async with self.sessions() as session:
            await cmd_mute(message("/mute 42 2h <reason>", user=ADMIN).as_(self.bot), self.bot, session)
            await cmd_addword(message("/addword <tag>", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIsNotNone(await session.get(ActionLog, 1))
        reply = self.transport.requests("sendMessage")[-1]
        self.assertIn("&lt;tag&gt;", reply.text)

    async def test_reason_html_and_bounds_are_handled_before_action(self):
        async with self.sessions() as session:
            await cmd_warn(message("/warn 42 <tag>&", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIn("&lt;tag&gt;&amp;", self.transport.requests("sendMessage")[-1].text)
            await cmd_mute(message("/mute 42 2h " + "x" * 513, user=ADMIN).as_(self.bot), self.bot, session)
            await cmd_addword(message("/addword " + "x" * 257, user=ADMIN).as_(self.bot), self.bot, session)
        self.assertFalse(self.transport.requests("restrictChatMember"))

    async def test_manual_mute_failure_is_logged(self):
        self.transport.failures["restrictChatMember"].append("bad <request>")
        async with self.sessions() as session:
            with self.assertLogs("bot.utils.actions", level="WARNING"):
                await cmd_mute(message("/mute 42 2h", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertEqual((await session.scalar(select(ActionLog))).action, "MUTE_FAILED")
        self.assertIn("&lt;request&gt;", self.transport.requests("sendMessage")[-1].text)

    async def test_purge_records_partial_submission_without_claiming_deletion(self):
        count = 0
        async def fail_second(method):
            nonlocal count
            count += 1
            if count == 2:
                self.transport.failures["deleteMessages"].append("cannot delete")
        self.transport.hooks["deleteMessages"] = fail_second
        async with self.sessions() as session:
            with self.assertLogs("bot.utils.actions", level="WARNING"):
                await cmd_purge(message("/purge", message_id=250, user=ADMIN,
                                        reply_to_message=message(message_id=1)).as_(self.bot), self.bot, session)
            row = await session.scalar(select(ActionLog))
            self.assertEqual(row.action, "PURGE_PARTIAL")
            self.assertEqual(json.loads(row.content)["submitted"], 100)
        self.assertEqual(len(self.transport.requests("deleteMessages")), 2)

    async def test_log_delivery_uses_plain_text_and_survives_api_error(self):
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
            chat.log_channel_id = CHAT_ID
            await session.commit()
            self.transport.failures["sendMessage"].append("cannot send")
            with self.assertLogs("bot.utils.actions", level="ERROR"):
                await log_action(self.bot, session, CHAT_ID, "<untrusted>&", action="TEST")
            self.assertEqual((await session.scalar(select(ActionLog))).action, "TEST")
        self.assertIsNone(self.transport.requests("sendMessage")[0].parse_mode)

    async def test_retention_removes_only_old_archive_rows(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=100)
        async with self.sessions() as session:
            await get_or_create_chat(session, CHAT_ID)
            session.add_all([
                StoredMessage(chat_id=CHAT_ID, message_id=1, created_at=old),
                StoredMessage(chat_id=CHAT_ID, message_id=2, created_at=now),
                ActionLog(chat_id=CHAT_ID, action="OLD", created_at=old),
                ActionLog(chat_id=CHAT_ID, action="NEW", created_at=now),
            ])
            await session.commit()
            await purge_archives(session, now - timedelta(days=90))
            self.assertEqual([r.message_id for r in await session.scalars(select(StoredMessage))], [2])
            self.assertEqual([r.action for r in await session.scalars(select(ActionLog))], ["NEW"])
