import json
from datetime import datetime, timedelta, timezone

from aiogram.types import CallbackQuery, Chat, MessageEntity, Update
from redis.exceptions import ConnectionError
from sqlalchemy import select

from bot.database.crud import add_banned_word, get_or_create_chat, purge_archives
from bot.database.models import ActionLog, StoredMessage, Warning
from bot.handlers.admin import cmd_addword, cmd_mute, cmd_purge, cmd_warn, cmd_warns
from bot.handlers.settings import cmd_setwelcome
from bot.handlers.moderation import auto_moderate
from bot.filters.content import contains_link
from bot.utils.actions import log_action
from config import get_settings
from main import ALLOWED_UPDATES, create_dispatcher
from support import ADMIN, CHAT_ID, USER, DatabaseCase, message

SPAM = "https://spam.dev"


class FloodCounter:
    def __init__(self):
        self.count = 0
        self.unavailable = False
        self.calls = []

    async def eval(self, *args):
        self.calls.append(args)
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
        self.counter.calls.clear()

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
        # Both flagged messages are deleted, but the mute is applied once (60 s guard).
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)

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

    async def test_configured_flood_limit_applies(self):
        await self.enable(antiflood_messages=2)
        for index in range(1, 4):
            await self.feed(message("hi", message_id=index))
        self.assertEqual(len(self.transport.requests("deleteMessage")), 1)

    async def test_flood_script_receives_per_chat_window_and_limit(self):
        await self.enable(antiflood_messages=7, antiflood_window_seconds=12)
        await self.feed(message("hi"))
        _, _, _, window_ms, limit, _ = self.counter.calls[0]
        self.assertEqual((window_ms, limit), (12000, 7))

    async def press(self, data, user=ADMIN):
        callback = CallbackQuery(id="1", from_user=user, chat_instance="x", data=data,
                                 message=message("menu", message_id=500))
        await self.dp.feed_update(self.bot, Update(update_id=1, callback_query=callback))

    async def value(self, field):
        async with self.sessions() as session:
            return getattr(await get_or_create_chat(session, CHAT_ID), field)

    async def test_toggle_mentions_button(self):
        await self.press("set:filter_mentions")
        self.assertTrue(await self.value("filter_mentions"))

    async def test_number_buttons_step_the_ladder(self):
        await self.enable(mute_minutes=45)  # not on the ladder
        await self.press("num:mute_minutes:+")
        self.assertEqual(await self.value("mute_minutes"), 60)
        await self.press("num:mute_minutes:-")
        self.assertEqual(await self.value("mute_minutes"), 30)
        await self.press("num:warn_limit:+")
        self.assertEqual(await self.value("warn_limit"), 4)
        self.assertTrue(self.transport.requests("editMessageReplyMarkup"))

    async def test_number_buttons_reject_bad_input(self):
        await self.enable(warn_limit=10)
        await self.press("num:warn_limit:-", user=USER)  # not an admin
        await self.press("num:chat_id:+")
        await self.press("num:warn_limit:999")
        await self.press("num:warn_limit:+")  # top of the ladder
        self.assertFalse(self.transport.requests("editMessageReplyMarkup"))
        self.assertIn("предел", self.transport.requests("answerCallbackQuery")[-1].text)
        await self.press("num:warn_limit:.")
        self.assertEqual(await self.value("warn_limit"), 10)
        self.assertEqual(await self.value("chat_id"), CHAT_ID)


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

    async def configure(self, **fields):
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
            for field, value in fields.items():
                setattr(chat, field, value)
            await session.commit()

    async def actions(self):
        async with self.sessions() as session:
            return [r.action for r in await session.scalars(select(ActionLog).order_by(ActionLog.id))]

    async def warning_rows(self):
        async with self.sessions() as session:
            return len(list(await session.scalars(select(Warning))))

    async def warn(self, count=1):
        async with self.sessions() as session:
            for index in range(count):
                await cmd_warn(message("/warn 42 spam", message_id=index + 1, user=ADMIN).as_(self.bot), self.bot, session)

    async def test_warn_escalates_from_mute_to_ban(self):
        await self.configure(warn_limit=2, ban_after_mutes=1)
        await self.warn(2)
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)
        self.assertFalse(self.transport.requests("banChatMember"))
        await self.warn(2)
        self.assertEqual(len(self.transport.requests("banChatMember")), 1)
        self.assertEqual(await self.actions(), ["WARN", "WARN_MUTE", "WARN", "WARN_BAN"])
        self.assertEqual(await self.warning_rows(), 0)

    async def test_old_mutes_do_not_count_towards_ban(self):
        await self.configure(warn_limit=1, ban_after_mutes=1)
        async with self.sessions() as session:
            session.add(ActionLog(chat_id=CHAT_ID, action="WARN_MUTE", target_id=42,
                                  created_at=datetime.now(timezone.utc) - timedelta(days=31)))
            await session.commit()
        await self.warn()
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)
        self.assertFalse(self.transport.requests("banChatMember"))

    async def test_expired_warnings_are_not_counted(self):
        await self.configure(warn_limit=2)
        async with self.sessions() as session:
            session.add(Warning(chat_id=CHAT_ID, user_id=42,
                                created_at=datetime.now(timezone.utc) - timedelta(days=31)))
            await session.commit()
            await cmd_warns(message("/warns 42").as_(self.bot), self.bot, session)
            self.assertIn(" 0 ", self.transport.requests("sendMessage")[-1].text)
        await self.warn()  # 1/2, not a mute
        self.assertFalse(self.transport.requests("restrictChatMember"))
        self.assertEqual(await self.actions(), ["WARN"])

    async def test_link_warning_toggle_and_cooldown(self):
        async with self.sessions() as session:
            await self.configure(filter_links=True)
            await auto_moderate(message(SPAM).as_(self.bot), self.bot, session)
            self.assertEqual(await self.warning_rows(), 0)
            await self.configure(warn_on_links=True)
            await auto_moderate(message(SPAM, message_id=2).as_(self.bot), self.bot, session)
            self.assertEqual(await self.warning_rows(), 1)
            self.assertEqual(len(self.transport.requests("sendMessage")), 1)
            await auto_moderate(message(SPAM, message_id=3).as_(self.bot), self.bot, session)
            self.assertEqual(await self.warning_rows(), 1)  # 60 s cooldown
            self.assertEqual(len(self.transport.requests("deleteMessage")), 3)

    async def test_flood_mute_uses_configured_minutes(self):
        await self.configure(flood_mute_minutes=30)
        async with self.sessions() as session:
            await auto_moderate(message().as_(self.bot), self.bot, session, flooding=True)
        until = self.transport.requests("restrictChatMember")[0].until_date
        delta = until - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=29) < delta <= timedelta(minutes=30))

    async def test_failed_escalation_keeps_warnings(self):
        await self.configure(filter_links=True, warn_on_links=True, warn_limit=1)
        self.transport.failures["restrictChatMember"].append("not enough rights")
        async with self.sessions() as session:
            with self.assertLogs("bot.utils.actions", level="WARNING"):
                await auto_moderate(message(SPAM).as_(self.bot), self.bot, session)
        self.assertEqual(await self.actions(), ["DELETE", "WARN_MUTE_FAILED"])
        self.assertEqual(await self.warning_rows(), 1)

    async def test_sender_chat_is_only_deleted(self):
        await self.configure(filter_links=True, warn_on_links=True)
        async with self.sessions() as session:
            sender = Chat(id=-100999, type="channel", title="Other")
            await auto_moderate(message(SPAM, user=None, sender_chat=sender).as_(self.bot), self.bot, session)
        self.assertEqual(len(self.transport.requests("deleteMessage")), 1)
        self.assertEqual(await self.warning_rows(), 0)
        self.assertFalse(self.transport.requests("restrictChatMember"))

    async def test_setwelcome_escapes_limits_and_resets(self):
        async with self.sessions() as session:
            await cmd_setwelcome(message("/setwelcome <b>Hi</b> {name}", user=ADMIN).as_(self.bot), self.bot, session)
            chat = await get_or_create_chat(session, CHAT_ID)
            self.assertEqual(chat.welcome_text, "&lt;b&gt;Hi&lt;/b&gt; {name}")
            await cmd_setwelcome(message("/setwelcome " + "<" * 300, user=ADMIN).as_(self.bot), self.bot, session)
            self.assertEqual(chat.welcome_text, "&lt;b&gt;Hi&lt;/b&gt; {name}")
            await cmd_setwelcome(message("/setwelcome", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIsNone(chat.welcome_text)

    async def test_flood_warn_mute_is_not_shorter_than_flood_mute(self):
        await self.configure(warn_on_flood=True, warn_limit=1, flood_mute_minutes=30, mute_minutes=1)
        async with self.sessions() as session:
            await auto_moderate(message().as_(self.bot), self.bot, session, flooding=True)
        restricts = self.transport.requests("restrictChatMember")
        self.assertEqual(len(restricts), 2)  # flood mute, then warn mute
        delta = restricts[-1].until_date - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=29) < delta <= timedelta(minutes=30))

    async def test_flood_burst_does_not_override_escalation(self):
        await self.configure(warn_on_flood=True, warn_limit=1, flood_mute_minutes=10, mute_minutes=60)
        async with self.sessions() as session:
            await auto_moderate(message().as_(self.bot), self.bot, session, flooding=True)
            await auto_moderate(message(message_id=2).as_(self.bot), self.bot, session, flooding=True)
        restricts = self.transport.requests("restrictChatMember")
        self.assertEqual(len(restricts), 2)
        self.assertGreater(restricts[-1].until_date - datetime.now(timezone.utc), timedelta(minutes=59))
        self.assertEqual(len(self.transport.requests("deleteMessage")), 2)

    async def test_failed_escalation_is_not_retried_within_cooldown(self):
        await self.configure(filter_links=True, warn_on_links=True, warn_limit=1)
        self.transport.failures["restrictChatMember"].append("not enough rights")
        async with self.sessions() as session:
            with self.assertLogs("bot.utils.actions", level="WARNING"):
                await auto_moderate(message(SPAM).as_(self.bot), self.bot, session)
            await auto_moderate(message(SPAM, message_id=2).as_(self.bot), self.bot, session)
        self.assertEqual(await self.warning_rows(), 1)
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)

    async def last_restrict_minutes(self):
        until = self.transport.requests("restrictChatMember")[-1].until_date
        return (until - datetime.now(timezone.utc)).total_seconds() / 60

    async def test_warn_mute_never_shortens_flood_mute_in_either_order(self):
        for flood_first in (True, False):
            with self.subTest(flood_first=flood_first):
                self.transport.calls.clear()
                async with self.engine.begin() as connection:
                    await connection.execute(ActionLog.__table__.delete())
                    await connection.execute(Warning.__table__.delete())
                await self.configure(filter_links=True, warn_on_links=True, warn_limit=1,
                                     flood_mute_minutes=30, mute_minutes=1)
                async with self.sessions() as session:
                    steps = [lambda: auto_moderate(message(SPAM, message_id=1).as_(self.bot), self.bot, session),
                             lambda: auto_moderate(message("hi", message_id=2).as_(self.bot), self.bot, session, flooding=True)]
                    for step in (steps if not flood_first else steps[::-1]):
                        await step()
                self.assertGreater(await self.last_restrict_minutes(), 29)

    async def test_failed_flood_mute_does_not_extend_warn_mute(self):
        await self.configure(filter_links=True, warn_on_links=True, warn_limit=1,
                             flood_mute_minutes=30, mute_minutes=1)
        self.transport.failures["restrictChatMember"].append("not enough rights")
        async with self.sessions() as session:
            with self.assertLogs("bot.utils.actions", level="WARNING"):
                await auto_moderate(message("hi", message_id=1).as_(self.bot), self.bot, session, flooding=True)
            await auto_moderate(message(SPAM, message_id=2).as_(self.bot), self.bot, session)
        self.assertEqual(await self.actions(), ["DELETE", "FLOOD_MUTE_FAILED", "DELETE", "WARN_MUTE"])
        self.assertLess(await self.last_restrict_minutes(), 2)
