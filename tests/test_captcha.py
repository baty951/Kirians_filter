import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiogram.types import CallbackQuery, ChatMemberLeft, ChatMemberRestricted, ChatMemberUpdated
from sqlalchemy import select

from bot.database.crud import get_or_create_chat
from bot.database.models import ActionLog, CaptchaChallenge
from bot.handlers.admin import cmd_pending
from bot.handlers.welcome import expire_captchas, on_captcha_answer, on_external_restriction, on_join
from bot.utils.actions import mute_user
from config import Settings
from support import ADMIN, CHAT_ID, USER, DatabaseCase, message, restricted_member, solve_captcha


class CaptchaTests(DatabaseCase):
    async def challenge(self, *, message_id=100, active=True, expired=False, attempts_left=1):
        async with self.sessions() as session:
            session.add(CaptchaChallenge(chat_id=CHAT_ID, user_id=USER.id, message_id=message_id,
                                         answer=5, active=active, attempts_left=attempts_left,
                                         expires_at=datetime.now(timezone.utc) + timedelta(seconds=-1 if expired else 120)))
            await session.commit()
        self.transport.members[USER.id] = restricted_member()

    def callback(self, *, message_id=100, answer=5):
        return CallbackQuery(id="callback", from_user=USER, chat_instance="chat",
                             data=f"captcha:{USER.id}:{answer}",
                             message=message(message_id=message_id).as_(self.bot)).as_(self.bot)

    async def respond(self, **kwargs):
        async with self.sessions() as session:
            await on_captcha_answer(self.callback(**kwargs), self.bot, session)

    async def test_stale_button_does_not_consume_new_challenge(self):
        await self.challenge(message_id=200)
        await self.respond(message_id=100)
        async with self.sessions() as session:
            current = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            self.assertTrue(current.active)
            self.assertEqual(current.message_id, 200)
        self.assertFalse(self.transport.requests("restrictChatMember"))

    async def test_success_consumes_challenge_and_replay_does_not_unmute(self):
        await self.challenge()
        await self.respond()
        await self.respond()
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(CaptchaChallenge, (CHAT_ID, USER.id)))

    async def test_failed_unmute_can_be_retried_from_another_session(self):
        await self.challenge()
        self.transport.failures["restrictChatMember"].append("not enough rights")
        with self.assertLogs("bot.utils.actions", level="WARNING"):
            await self.respond()
        async with self.sessions() as session:
            self.assertTrue((await session.get(CaptchaChallenge, (CHAT_ID, USER.id))).active)
        self.assertFalse(self.transport.requests("deleteMessage"))
        self.assertNotIn("Добро пожаловать", self.transport.requests("answerCallbackQuery")[-1].text)
        await self.respond()
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 2)

    async def test_member_lookup_error_keeps_retry_available(self):
        await self.challenge()
        self.transport.failures["getChatMember"].append("temporary error")
        await self.respond()
        async with self.sessions() as session:
            self.assertTrue((await session.get(CaptchaChallenge, (CHAT_ID, USER.id))).active)

    async def test_wrong_answer_and_timeout_keep_mute_ownership(self):
        await self.challenge()
        await self.respond(answer=6)
        async with self.sessions() as session:
            row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            self.assertFalse(row.active)
            row.active = True
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
        await expire_captchas(self.bot, self.sessions)
        async with self.sessions() as session:
            self.assertFalse((await session.get(CaptchaChallenge, (CHAT_ID, USER.id))).active)
        self.assertFalse(self.transport.requests("restrictChatMember"))

    async def test_wrong_answer_with_attempts_left_asks_a_new_question_in_place(self):
        await self.challenge(attempts_left=3)
        await self.respond(answer=6)
        async with self.sessions() as session:
            row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            self.assertTrue(row.active)
            self.assertEqual((row.attempts_left, row.message_id), (2, 100))
            new_answer = row.answer
        edits = self.transport.requests("editMessageText")
        self.assertEqual(len(edits), 1)
        # The stored answer must be the one for the question now on screen, and offered as a button.
        self.assertEqual(new_answer, solve_captcha(edits[0].text, edits[0].reply_markup))
        self.assertFalse(self.transport.requests("deleteMessage") or self.transport.requests("restrictChatMember"))
        await self.respond(answer=new_answer)  # the new question is answerable and unmutes
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(CaptchaChallenge, (CHAT_ID, USER.id)))

    async def test_correct_answer_turns_the_captcha_into_the_welcome(self):
        await self.challenge()
        async with self.sessions() as session:
            (await get_or_create_chat(session, CHAT_ID)).welcome_text = "Hi, {name}!"
            await session.commit()
        await self.respond()
        edit = self.transport.requests("editMessageText")[-1]
        self.assertTrue(edit.text.startswith("Hi, <a href="))
        self.assertIsNone(edit.reply_markup)  # no buttons left
        self.assertFalse(self.transport.requests("deleteMessage"))
        self.assertFalse(self.transport.requests("sendMessage"))
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)

    async def test_default_welcome_and_disabled_welcome(self):
        await self.challenge()
        await self.respond()
        self.assertIn("Добро пожаловать", self.transport.requests("editMessageText")[-1].text)
        self.transport.calls.clear()
        await self.challenge()
        async with self.sessions() as session:
            (await get_or_create_chat(session, CHAT_ID)).welcome_enabled = False
            await session.commit()
        await self.respond()
        self.assertFalse(self.transport.requests("editMessageText"))
        self.assertTrue(self.transport.requests("deleteMessage"))  # the captcha message just disappears

    async def test_failed_welcome_edit_falls_back_to_a_new_message(self):
        await self.challenge()
        self.transport.failures["editMessageText"].append("message can't be edited")
        with self.assertLogs("bot.utils.actions", level="WARNING"):
            await self.respond()
        self.assertTrue(self.transport.requests("deleteMessage"))
        self.assertIn("Добро пожаловать", self.transport.requests("sendMessage")[-1].text)

    async def test_wrong_answers_never_show_the_welcome(self):
        await self.challenge(attempts_left=1)
        await self.respond(answer=6)
        self.assertFalse(self.transport.requests("editMessageText"))
        self.assertFalse(self.transport.requests("sendMessage"))

    async def test_attempts_run_out_and_leave_the_member_muted(self):
        await self.challenge(attempts_left=2)
        await self.respond(answer=99)
        async with self.sessions() as session:
            answer = (await session.get(CaptchaChallenge, (CHAT_ID, USER.id))).answer
        await self.respond(answer=99 if answer != 99 else 98)
        async with self.sessions() as session:
            row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            self.assertFalse(row.active)
        self.assertTrue(self.transport.requests("deleteMessage"))
        self.assertFalse(self.transport.requests("restrictChatMember"))
        await self.respond(answer=answer)  # a failed challenge can no longer be answered
        self.assertFalse(self.transport.requests("restrictChatMember"))

    async def test_failed_question_update_does_not_consume_an_attempt(self):
        await self.challenge(attempts_left=3)
        self.transport.failures["editMessageText"].append("message is not modified")
        await self.respond(answer=6)
        async with self.sessions() as session:
            row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            self.assertEqual((row.attempts_left, row.answer), (3, 5))

    async def test_join_gives_the_chat_configured_attempts(self):
        async with self.sessions() as session:
            (await get_or_create_chat(session, CHAT_ID)).captcha_attempts = 2
            await session.commit()
            await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
            self.assertEqual((await session.get(CaptchaChallenge, (CHAT_ID, USER.id))).attempts_left, 2)

    async def test_pending_lists_only_failed_challenges_for_admins(self):
        async with self.sessions() as session:
            session.add(CaptchaChallenge(chat_id=CHAT_ID, user_id=111, message_id=1, answer=5, active=False,
                                         expires_at=datetime.now(timezone.utc)))
            session.add(CaptchaChallenge(chat_id=CHAT_ID, user_id=222, message_id=2, answer=5, active=True,
                                         expires_at=datetime.now(timezone.utc)))
            await session.commit()
            await cmd_pending(message("/pending", user=USER).as_(self.bot), self.bot, session)
            self.assertIn("⛔", self.transport.requests("sendMessage")[-1].text)
            await cmd_pending(message("/pending", user=ADMIN).as_(self.bot), self.bot, session)
            reply = self.transport.requests("sendMessage")[-1].text
            self.assertIn("user?id=111", reply)
            self.assertNotIn("222", reply)

    async def test_join_post_failure_does_not_mute(self):
        self.transport.failures["sendMessage"].append("cannot send")
        async with self.sessions() as session:
            with self.assertLogs("bot.handlers.welcome", level="ERROR"):
                await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
        self.assertFalse(self.transport.requests("restrictChatMember"))
        async with self.sessions() as session:
            self.assertFalse(list(await session.scalars(select(CaptchaChallenge))))

    async def test_join_persists_challenge_before_restriction(self):
        async def check_persisted(method):
            async with self.sessions() as session:
                row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
                self.assertIsNotNone(row)
                self.assertTrue(row.active)
        self.transport.hooks["restrictChatMember"] = check_persisted
        async with self.sessions() as session:
            await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 1)
        async with self.sessions() as session:
            row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            await on_captcha_answer(self.callback(message_id=row.message_id, answer=row.answer), self.bot, session)
        self.assertEqual(len(self.transport.requests("restrictChatMember")), 2)

    async def test_protected_users_admins_and_existing_manual_mutes_are_skipped(self):
        settings = Settings(BOT_TOKEN="123456:ABC-test-token", PROTECTED_IDS="42", _env_file=None)
        async with self.sessions() as session:
            with patch("bot.utils.helpers.get_settings", return_value=settings):
                await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
            await on_join(message(new_chat_members=[ADMIN]).as_(self.bot), self.bot, session)
            await get_or_create_chat(session, CHAT_ID)
            session.add(ActionLog(chat_id=CHAT_ID, action="MUTE", target_id=USER.id))
            await session.commit()
            self.transport.members[USER.id] = restricted_member()
            await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
        self.assertFalse(self.transport.requests("sendMessage"))
        self.assertFalse(self.transport.requests("restrictChatMember"))

    async def test_legacy_captcha_mute_gets_new_captcha_on_rejoin(self):
        self.transport.members[USER.id] = restricted_member()
        async with self.sessions() as session:
            await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
            row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            await on_captcha_answer(self.callback(message_id=row.message_id, answer=row.answer), self.bot, session)
        self.assertTrue(self.transport.requests("restrictChatMember")[-1].permissions.can_send_messages)

    async def test_rejoin_replaces_failed_question_but_stale_answer_keeps_new_one(self):
        await self.challenge(active=False)
        async with self.sessions() as session:
            await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
        await self.respond(message_id=100)
        async with self.sessions() as session:
            row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
            self.assertTrue(row.active)
            self.assertNotEqual(row.message_id, 100)

    async def test_old_expiry_snapshot_does_not_expire_replacement(self):
        await self.challenge(expired=True)
        from bot.utils.helpers import restriction_lock
        async with restriction_lock(CHAT_ID, USER.id):
            expiry = asyncio.create_task(expire_captchas(self.bot, self.sessions))
            # Wait until the expiry task has read its snapshot and reached the lock.
            for _ in range(100):
                if expiry.done():
                    self.fail("Expiry did not wait for the restriction lock")
                await asyncio.sleep(0.001)
                lock = restriction_lock(CHAT_ID, USER.id)
                if lock._waiters:
                    break
            else:
                self.fail("Expiry did not reach the restriction lock")
            async with self.sessions() as session:
                row = await session.get(CaptchaChallenge, (CHAT_ID, USER.id))
                row.message_id = 200
                row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=120)
                await session.commit()
        await asyncio.wait_for(expiry, 5)
        async with self.sessions() as session:
            self.assertTrue((await session.get(CaptchaChallenge, (CHAT_ID, USER.id))).active)
        self.assertFalse(self.transport.requests("deleteMessage"))

    async def test_manual_mute_wins_both_interleavings(self):
        for captcha_first in (True, False):
            with self.subTest(captcha_first=captcha_first):
                await self.challenge()
                entered, release = asyncio.Event(), asyncio.Event()
                async def pause(method):
                    entered.set()
                    await release.wait()
                self.transport.hooks["restrictChatMember"] = pause
                async def punish():
                    async with self.sessions() as session:
                        return await mute_user(self.bot, CHAT_ID, USER.id, timedelta(hours=2), session=session)
                first = asyncio.create_task(self.respond() if captcha_first else punish())
                await asyncio.wait_for(entered.wait(), 5)
                second = asyncio.create_task(punish() if captcha_first else self.respond())
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 5)
                self.assertFalse(self.transport.requests("restrictChatMember")[-1].permissions.can_send_messages)
                async with self.sessions() as session:
                    self.assertIsNone(await session.get(CaptchaChallenge, (CHAT_ID, USER.id)))
                self.transport.hooks.clear()

    async def test_externally_changed_duration_cannot_be_lifted(self):
        await self.challenge()
        self.transport.members[USER.id] = restricted_member(until_date=datetime.now(timezone.utc) + timedelta(hours=1))
        await self.respond()
        self.assertFalse(self.transport.requests("restrictChatMember"))

    async def test_leaving_keeps_ownership_but_admin_change_invalidates_it(self):
        await self.challenge()
        required = {field: False for field, info in ChatMemberRestricted.model_fields.items()
                    if info.is_required() and field not in ("user", "until_date")}
        old = ChatMemberRestricted(user=USER, until_date=0, **(required | {"is_member": True}))
        event = ChatMemberUpdated(chat=message().chat, from_user=USER, date=datetime.now(timezone.utc),
                                  old_chat_member=old, new_chat_member=ChatMemberLeft(user=USER))
        async with self.sessions() as session:
            await on_external_restriction(event, self.bot, session)
            self.assertIsNotNone(await session.get(CaptchaChallenge, (CHAT_ID, USER.id)))
            left_restricted = event.model_copy(update={"new_chat_member": old.model_copy(update={"is_member": False})})
            await on_external_restriction(left_restricted, self.bot, session)
            self.assertIsNotNone(await session.get(CaptchaChallenge, (CHAT_ID, USER.id)))
            changed = event.model_copy(update={"from_user": ADMIN, "new_chat_member": old})
            await on_external_restriction(changed, self.bot, session)
            self.assertIsNone(await session.get(CaptchaChallenge, (CHAT_ID, USER.id)))
