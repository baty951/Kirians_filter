from aiogram.types import CallbackQuery

from bot.database.crud import get_or_create_chat
from bot.handlers.settings import (
    NUMBERS, SECTIONS, TOGGLES, _SECTION_OF, _build_keyboard, _build_menu, cmd_settings, open_menu,
)
from support import ADMIN, CHAT_ID, USER, DatabaseCase, message


class SettingsMenuTests(DatabaseCase):
    def test_every_setting_is_in_exactly_one_section(self):
        placed = [field for _, fields in SECTIONS.values() for field in fields]
        self.assertEqual(sorted(placed), sorted([*TOGGLES, *NUMBERS]))
        self.assertEqual(len(placed), len(set(placed)))
        self.assertEqual(set(_SECTION_OF), set(placed))

    async def test_callback_data_fits_telegram_limit(self):
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
        keyboards = [_build_menu(), *(_build_keyboard(chat, key) for key in SECTIONS)]
        for keyboard in keyboards:
            for row in keyboard.inline_keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def open(self, data, user=ADMIN):
        callback = CallbackQuery(id="cb", from_user=user, chat_instance="chat", data=data,
                                 message=message("menu", message_id=500).as_(self.bot)).as_(self.bot)
        async with self.sessions() as session:
            await open_menu(callback, self.bot, session)

    async def test_settings_command_shows_sections_only(self):
        async with self.sessions() as session:
            await cmd_settings(message("/settings", user=ADMIN).as_(self.bot), self.bot, session)
        data = [b.callback_data for row in self.transport.requests("sendMessage")[-1].reply_markup.inline_keyboard
                for b in row]
        self.assertEqual(data, [f"menu:{key}" for key in SECTIONS])

    async def test_navigation_between_menu_and_sections(self):
        await self.open("menu:flood")
        edit = self.transport.requests("editMessageText")[-1]
        data = [b.callback_data for row in edit.reply_markup.inline_keyboard for b in row]
        self.assertIn("set:antiflood_enabled", data)
        self.assertIn("num:flood_mute_minutes:+", data)
        self.assertNotIn("set:filter_links", data)  # belongs to another section
        self.assertEqual(data[-1], "menu:main")
        await self.open("menu:main")
        back = self.transport.requests("editMessageText")[-1]
        self.assertEqual([b.callback_data for row in back.reply_markup.inline_keyboard for b in row],
                         [f"menu:{key}" for key in SECTIONS])

    async def test_only_admins_navigate_and_unknown_sections_are_ignored(self):
        await self.open("menu:filters", user=USER)
        await self.open("menu:nonsense")
        self.assertFalse(self.transport.requests("editMessageText"))
