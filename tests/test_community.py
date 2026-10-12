from aiogram.types import CallbackQuery, User
from sqlalchemy import select

from bot.database.crud import get_or_create_chat, is_approved
from bot.database.models import ActionLog, Warning
from bot.handlers import community
from bot.handlers.community import (
    cmd_approve, cmd_approved, cmd_report, cmd_unapprove, on_report_action,
)
from bot.handlers.moderation import auto_moderate
from support import ADMIN, CHAT_ID, USER, DatabaseCase, FakeRedis, message

OFFENDER = User(id=77, is_bot=False, first_name="Offender")
LINK = "see https://spam.example/x"


class ApproveTests(DatabaseCase):
    async def test_approved_users_skip_automatic_moderation(self):
        async with self.sessions() as session:
            (await get_or_create_chat(session, CHAT_ID)).filter_links = True
            await session.commit()
            self.assertTrue(await auto_moderate(message(LINK).as_(self.bot), self.bot, session))
            reply = message("hi", user=USER, message_id=5)
            await cmd_approve(message("/approve", user=ADMIN, reply_to_message=reply).as_(self.bot), self.bot, session)
            self.assertTrue(await is_approved(session, CHAT_ID, USER.id))
            self.assertFalse(await auto_moderate(message(LINK).as_(self.bot), self.bot, session))
            await cmd_unapprove(message(f"/unapprove {USER.id}", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertTrue(await auto_moderate(message(LINK).as_(self.bot), self.bot, session))
            actions = [row.action for row in await session.scalars(select(ActionLog).order_by(ActionLog.id))]
            self.assertEqual([a for a in actions if "APPROVE" in a], ["APPROVE", "UNAPPROVE"])

    async def test_commands_are_admin_only_and_idempotent(self):
        async with self.sessions() as session:
            await cmd_approve(message(f"/approve {OFFENDER.id}", user=USER).as_(self.bot), self.bot, session)
            self.assertIn("⛔", self.transport.requests("sendMessage")[-1].text)
            self.assertFalse(await is_approved(session, CHAT_ID, OFFENDER.id))
            for _ in range(2):
                await cmd_approve(message(f"/approve {OFFENDER.id}", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIn("Уже", self.transport.requests("sendMessage")[-1].text)
            await cmd_approved(message("/approved", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIn(f"user?id={OFFENDER.id}", self.transport.requests("sendMessage")[-1].text)


class ReportTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        community._handled_reports.clear()
        self.redis = FakeRedis()

    async def report(self, *, reporter=USER, target=OFFENDER, text="/report spam"):
        reported = message("bad text", user=target, message_id=7)
        await cmd_report(message(text, user=reporter, message_id=8, reply_to_message=reported).as_(self.bot),
                         self.bot, self.redis)

    def last_text(self):
        return self.transport.requests("sendMessage")[-1].text

    async def test_report_pings_admins_and_offers_actions(self):
        await self.report()
        call = self.transport.requests("sendMessage")[-1]
        self.assertIn("Жалоба", call.text)
        self.assertIn(f"user?id={ADMIN.id}", call.text)
        self.assertIn("spam", call.text)
        data = [b.callback_data for row in call.reply_markup.inline_keyboard for b in row]
        self.assertEqual(sorted(data), sorted(f"rpt:{a}:7:{OFFENDER.id}" for a in "dwbi"))
        self.assertTrue(self.transport.requests("deleteMessage"))  # the /report command itself

    async def test_report_rules(self):
        await self.report(target=ADMIN)
        self.assertIn("нельзя", self.last_text())
        await self.report(target=USER)
        self.assertIn("нельзя", self.last_text())
        await cmd_report(message("/report").as_(self.bot), self.bot, self.redis)
        self.assertIn("Ответьте", self.last_text())
        self.assertFalse(any("Жалоба" in c.text for c in self.transport.requests("sendMessage")))
        await self.report()
        await self.report()  # cooldown per reporter
        self.assertIn("Подождите", self.last_text())
        self.redis.unavailable = True  # a broken cooldown must not block reports
        await self.report(reporter=ADMIN, target=OFFENDER)
        self.assertIn("Жалоба", self.last_text())

    def press(self, data, user=ADMIN, message_id=500):
        return CallbackQuery(id="cb", from_user=user, chat_instance="chat", data=data,
                             message=message("🚩 Жалоба", message_id=message_id).as_(self.bot)).as_(self.bot)

    async def act(self, action, **kwargs):
        async with self.sessions() as session:
            await on_report_action(self.press(f"rpt:{action}:7:{OFFENDER.id}", **kwargs), self.bot, session)

    async def test_delete_and_ignore(self):
        await self.act("i")
        self.assertFalse(self.transport.requests("deleteMessage"))
        await self.act("d", message_id=501)
        deleted = self.transport.requests("deleteMessage")
        self.assertEqual([c.message_id for c in deleted], [7])
        edit = self.transport.requests("editMessageText")[-1]
        self.assertIn("сообщение удалено", edit.text)

    async def test_warn_and_ban_actions(self):
        await self.act("w")
        async with self.sessions() as session:
            self.assertEqual(len(list(await session.scalars(select(Warning)))), 1)
        self.assertEqual([c.message_id for c in self.transport.requests("deleteMessage")], [7])
        await self.act("b", message_id=501)
        self.assertEqual([c.user_id for c in self.transport.requests("banChatMember")], [OFFENDER.id])

    async def test_only_admins_act_and_a_report_is_handled_once(self):
        await self.act("b", user=USER)
        self.assertFalse(self.transport.requests("banChatMember"))
        await self.act("b")
        await self.act("b")  # second admin pressing the same report
        self.assertEqual(len(self.transport.requests("banChatMember")), 1)

    async def test_bad_data_and_protected_targets_are_rejected(self):
        async with self.sessions() as session:
            for data in ("rpt:x:7:77", "rpt:b:7", "rpt:b:abc:77", "rpt:b:7:-1"):
                await on_report_action(self.press(data), self.bot, session)
            await on_report_action(self.press(f"rpt:b:7:{ADMIN.id}"), self.bot, session)  # an admin cannot be banned
        self.assertFalse(self.transport.requests("banChatMember") or self.transport.requests("deleteMessage"))

    async def test_failed_ban_is_reported_not_raised(self):
        self.transport.failures["banChatMember"].append("not enough rights")
        with self.assertLogs("bot.utils.actions", level="WARNING"):
            await self.act("b")
        self.assertIn("бан не удался", self.transport.requests("editMessageText")[-1].text)
