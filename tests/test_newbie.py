from datetime import datetime, timedelta, timezone

from aiogram.types import ChatMemberLeft, ChatMemberMember, ChatMemberUpdated
from sqlalchemy import select

from bot.database.crud import get_or_create_chat, is_newbie, purge_archives, record_join
from bot.database.models import MemberJoin, Warning
from bot.handlers.moderation import auto_moderate
from bot.handlers.welcome import on_external_restriction, on_join
from support import CHAT_ID, USER, DatabaseCase, message

PHOTO = {"photo": [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]}
CONTACT = {"contact": {"phone_number": "1", "first_name": "a"}}
DICE = {"dice": {"emoji": "🎲", "value": 1}}
FORWARD = {"forward_origin": {"type": "hidden_user", "date": datetime.now(timezone.utc), "sender_user_name": "x"}}


class NewbieTests(DatabaseCase):
    async def setup_chat(self, session, *, joined_hours_ago=1, **settings):
        chat = await get_or_create_chat(session, CHAT_ID)
        chat.filter_newbie = True
        for field, value in settings.items():
            setattr(chat, field, value)
        await session.commit()
        if joined_hours_ago is not None:
            await record_join(session, CHAT_ID, USER.id)
            join = await session.get(MemberJoin, (CHAT_ID, USER.id))
            join.joined_at = datetime.now(timezone.utc) - timedelta(hours=joined_hours_ago)
            await session.commit()

    async def moderate(self, session, text=None, **extra):
        return await auto_moderate(message(text, **extra).as_(self.bot), self.bot, session)

    async def test_non_text_from_a_newbie_is_deleted_but_text_is_not(self):
        async with self.sessions() as session:
            await self.setup_chat(session)
            for name, payload in (("photo", PHOTO), ("contact", CONTACT), ("dice", DICE), ("forward", FORWARD)):
                with self.subTest(content=name):
                    self.assertTrue(await self.moderate(session, **payload))
            self.assertFalse(await self.moderate(session, "hello"))

    async def test_old_members_unknown_members_and_disabled_mode_are_untouched(self):
        async with self.sessions() as session:
            await self.setup_chat(session, joined_hours_ago=25)  # newbie_hours defaults to 24
            self.assertFalse(await self.moderate(session, **PHOTO))
            await session.delete(await session.get(MemberJoin, (CHAT_ID, USER.id)))
            await session.commit()
            self.assertFalse(await self.moderate(session, **PHOTO))  # joined before the feature: no record
            await record_join(session, CHAT_ID, USER.id)
            self.assertTrue(await self.moderate(session, **PHOTO))
            (await get_or_create_chat(session, CHAT_ID)).filter_newbie = False
            await session.commit()
            self.assertFalse(await self.moderate(session, **PHOTO))

    async def test_term_follows_newbie_hours_and_rejoin_resets_it(self):
        async with self.sessions() as session:
            await self.setup_chat(session, joined_hours_ago=30, newbie_hours=48)
            self.assertTrue(await is_newbie(session, CHAT_ID, USER.id, 48))
            self.assertFalse(await is_newbie(session, CHAT_ID, USER.id, 24))
            await record_join(session, CHAT_ID, USER.id)
            self.assertTrue(await is_newbie(session, CHAT_ID, USER.id, 1))

    async def test_warning_is_optional(self):
        async with self.sessions() as session:
            await self.setup_chat(session)
            self.assertTrue(await self.moderate(session, **PHOTO))
            self.assertFalse(list(await session.scalars(select(Warning))))
            (await get_or_create_chat(session, CHAT_ID)).warn_on_newbie = True
            await session.commit()
            self.assertTrue(await self.moderate(session, **PHOTO))
            self.assertEqual(len(list(await session.scalars(select(Warning)))), 1)

    async def test_joins_are_recorded_from_service_message_and_chat_member_update(self):
        async with self.sessions() as session:
            (await get_or_create_chat(session, CHAT_ID)).captcha_enabled = False
            await session.commit()
            await on_join(message(new_chat_members=[USER]).as_(self.bot), self.bot, session)
            self.assertIsNotNone(await session.get(MemberJoin, (CHAT_ID, USER.id)))
            await session.delete(await session.get(MemberJoin, (CHAT_ID, USER.id)))
            await session.commit()
            event = ChatMemberUpdated(
                chat=message().chat, from_user=USER, date=datetime.now(timezone.utc),
                old_chat_member=ChatMemberLeft(user=USER), new_chat_member=ChatMemberMember(user=USER),
            )
            await on_external_restriction(event, self.bot, session)
            self.assertIsNotNone(await session.get(MemberJoin, (CHAT_ID, USER.id)))
            # A member changing status (not joining) must not reset the clock.
            await session.delete(await session.get(MemberJoin, (CHAT_ID, USER.id)))
            await session.commit()
            same = event.model_copy(update={"old_chat_member": ChatMemberMember(user=USER)})
            await on_external_restriction(same, self.bot, session)
            self.assertIsNone(await session.get(MemberJoin, (CHAT_ID, USER.id)))

    async def test_old_joins_are_purged(self):
        async with self.sessions() as session:
            await record_join(session, CHAT_ID, USER.id)
            join = await session.get(MemberJoin, (CHAT_ID, USER.id))
            join.joined_at = datetime.now(timezone.utc) - timedelta(days=100)
            await session.commit()
            await purge_archives(session, datetime.now(timezone.utc) - timedelta(days=90))
            self.assertIsNone(await session.get(MemberJoin, (CHAT_ID, USER.id)))
