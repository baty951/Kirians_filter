from aiogram.types import MessageEntity
from sqlalchemy import select

from bot.database.crud import add_banned_word, get_or_create_chat
from bot.database.models import Warning
from bot.filters.content import (
    contains_link, contains_mention, find_banned_word, fold, homoglyph_variants, normalize_domain,
)
from bot.handlers.admin import cmd_addword, cmd_allowdomain, cmd_delword, cmd_denydomain
from bot.handlers.moderation import auto_moderate
from support import ADMIN, CHAT_ID, USER, DatabaseCase, message

ZW = "​"
SHY = "­"


def hit(text, *patterns, regex=False):
    return find_banned_word(text, [(p, regex) for p in patterns])


class WordTests(DatabaseCase):
    def test_homoglyphs_and_invisible_chars(self):
        for text, pattern in (
            ("пpивет", "привет"),  # Latin p
            (f"f{ZW}uck", "fuck"),
            (f"шл{SHY}юха", "шлюх"),
            ("fuck you", "fuck"),
            ("fuсk", "fuck"),  # Cyrillic с
            ("cockа", "cock"),  # Cyrillic а
            ("Ёбаный", "ёб"),
        ):
            with self.subTest(text=text):
                self.assertEqual(hit(text, pattern), pattern)

    def test_repeats_are_tolerated(self):
        for text, pattern in (("шлюхааа", "шлюх"), ("сууука", "сука"), ("пииииздец", "пизд"), ("aaass", "ass")):
            with self.subTest(text=text):
                self.assertEqual(hit(text, pattern), pattern)

    def test_no_widening_or_inner_matches(self):
        for text, pattern in (
            ("class", "ass"), ("pass", "ass"), (f"cl{ZW}ass", "ass"),
            ("content", "coon"), ("pistol", "piss"), ("сани", "ссан"), ("Niger", "nigger"),
        ):
            with self.subTest(text=text):
                self.assertIsNone(hit(text, pattern))
        self.assertEqual(hit("ass", "ass"), "ass")

    def test_regex_patterns(self):
        self.assertEqual(hit("хуй", "х[уy]й", regex=True), "х[уy]й")
        self.assertEqual(hit("xyй", "хуй", regex=True), "хуй")

    def test_mixed_tokens_are_converted_in_both_directions(self):
        for text, pattern in (("cyка", "сука"), ("cockа", "cock"), ("fuck", "fuck"), ("хуйnя", "хуй")):
            with self.subTest(text=text):
                self.assertEqual(hit(text, pattern), pattern)

    def test_single_script_tokens_are_untouched(self):
        self.assertEqual(homoglyph_variants("copy"), ("copy", "copy"))
        self.assertEqual(homoglyph_variants("привет"), ("привет", "привет"))
        self.assertEqual(homoglyph_variants("хуйnя"), ("хуйnя", "хуйnя"))  # partly convertible

    def test_nfkc_expansion_is_bounded_and_keeps_the_tail(self):
        for text in ("ﬃ" * 1400 + " fuck", "ﷺ" * 4000 + " fuck"):
            with self.subTest(text=text[:2]):
                self.assertEqual(hit(text, "fuck"), "fuck")
                self.assertLessEqual(len(fold(text[:4096])), 4 * 4096)
        self.assertIsNone(hit("ﷺ" * 4096, "ass", "fuck", "шлюх"))

    def test_very_short_stems_match_whole_words_only(self):
        for text, pattern in (("customer", "cu"), ("current", "cu"), ("كسر", "كس"), ("زبون", "زب")):
            with self.subTest(text=text):
                self.assertIsNone(hit(text, pattern))
        for text, pattern in (("cu", "cu"), ("ты cu!", "cu"), ("كس", "كس"), ("cuuu", "cu")):
            with self.subTest(text=text):
                self.assertEqual(hit(text, pattern), pattern)
        self.assertEqual(hit("cuzãozinho", "cuzão"), "cuzão")  # other stems keep prefix matching
        self.assertEqual(hit("ебнутый", "ёб"), "ёб")  # real short prefixes are not affected
        self.assertEqual(hit("oçlar", "oç"), "oç")

    def test_patterns_that_match_everything_are_ignored(self):
        for pattern, regex in ((ZW, False), (f"{ZW}{SHY}", False), ("  ", False), (".*", True), ("a*", True)):
            with self.subTest(pattern=pattern):
                self.assertIsNone(hit("hello world", pattern, regex=regex))

    def test_fold_keeps_cyrillic_and_composes_accents(self):
        self.assertEqual(hit("хуй", "хуй"), "хуй")
        self.assertEqual(hit("ёбать", "ёб"), "ёб")
        self.assertIn("й", fold("и​̆"))  # invisible char between base and breve


class LinkTests(DatabaseCase):
    def test_basic(self):
        self.assertFalse(contains_link("@username"))
        self.assertTrue(contains_link("t.me/x"))

    def test_allow_list(self):
        allowed = ["youtube.com"]
        url = lambda text: [MessageEntity(type="url", offset=0, length=len(text))]  # noqa: E731
        for text in (
            "https://youtube.com/watch", "https://m.youtube.com", "HTTPS://YouTube.com",
            "https://youtube.com:8080/x", "youtube.com.",
        ):
            with self.subTest(text=text):
                self.assertFalse(contains_link(text, url(text), allowed))
        for text in (
            "https://evil-youtube.com", "https://youtube.com.evil.io", "https://уoutube.com",
            "https://youtube.com@evil.io", "https://youtube.com https://evil.io", "tg://user?id=1",
            r"https://evil.io\@youtube.com",
        ):
            with self.subTest(text=text):
                self.assertTrue(contains_link(text, url(text), allowed))

    def test_text_link_uses_real_target(self):
        entity = MessageEntity(type="text_link", offset=0, length=11, url="https://evil.io")
        self.assertTrue(contains_link("youtube.com", [entity], ["youtube.com"]))

    def test_text_link_labels_are_not_links(self):
        def link(offset, length, url):
            return MessageEntity(type="text_link", offset=offset, length=length, url=url)
        allowed = ["youtube.com"]
        label = "https://evil.io"
        self.assertFalse(contains_link(label, [link(0, len(label), "https://youtube.com/x")], allowed))
        self.assertTrue(contains_link("click", [link(0, 5, "https://evil.io")], allowed))
        # Emoji shifts UTF-16 offsets; the plain link after the label must still be found.
        text = "\U0001f600 " + label + " https://evil.io"
        self.assertTrue(contains_link(text, [link(3, len(label), "https://youtube.com/x")], allowed))
        # A url entity next to a text_link is still checked.
        text = "label https://evil.io"
        entities = [link(0, 5, "https://youtube.com"), MessageEntity(type="url", offset=6, length=15)]
        self.assertTrue(contains_link(text, entities, allowed))

    def test_utf16_offsets(self):
        entity = MessageEntity(type="url", offset=3, length=11)
        self.assertFalse(contains_link("\U0001f600 example.dev", [entity], ["example.dev"]))

    def test_normalize_domain(self):
        self.assertEqual(normalize_domain("https://WWW.YouTube.com/x"), "youtube.com")
        self.assertIsNone(normalize_domain("com"))
        self.assertIsNone(normalize_domain(""))
        self.assertEqual(normalize_domain("пример.рф"), "xn--e1afmkfd.xn--p1ai")

    def test_mentions(self):
        self.assertTrue(contains_mention("hi @spammer_bot"))
        self.assertFalse(contains_mention("mail@example.com"))
        self.assertFalse(contains_mention("/help@test_bot"))


class ChatFilterTests(DatabaseCase):
    async def configure(self, **fields):
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
            for field, value in fields.items():
                setattr(chat, field, value)
            await session.commit()

    async def test_mentions_are_separate_from_links(self):
        await self.configure(filter_links=True)
        async with self.sessions() as session:
            self.assertFalse(await auto_moderate(message("@spammer_bot").as_(self.bot), self.bot, session))
            await self.configure(filter_mentions=True)
            self.assertTrue(await auto_moderate(message("@spammer_bot", message_id=2).as_(self.bot), self.bot, session))
            self.assertFalse(await auto_moderate(message("mail@example.com", message_id=3).as_(self.bot), self.bot, session))

    async def test_warn_on_mentions(self):
        await self.configure(filter_mentions=True, warn_on_mentions=True)
        async with self.sessions() as session:
            await auto_moderate(message("@spammer_bot").as_(self.bot), self.bot, session)
            self.assertEqual(len(list(await session.scalars(select(Warning)))), 1)

    async def test_allowed_domain_passes_link_filter(self):
        await self.configure(filter_links=True, allowed_domains="youtube.com")
        async with self.sessions() as session:
            self.assertFalse(await auto_moderate(message("https://youtube.com/x").as_(self.bot), self.bot, session))
            self.assertTrue(await auto_moderate(message("https://evil.io", message_id=2).as_(self.bot), self.bot, session))

    async def test_caption_links_use_the_allow_list(self):
        await self.configure(filter_links=True, allowed_domains="youtube.com")
        def caption(text, mid):
            entity = MessageEntity(type="url", offset=0, length=len(text))
            return message(None, message_id=mid, caption=text, caption_entities=[entity]).as_(self.bot)
        async with self.sessions() as session:
            self.assertFalse(await auto_moderate(caption("https://youtube.com/x", 1), self.bot, session))
            self.assertTrue(await auto_moderate(caption("https://evil.io", 2), self.bot, session))

    async def test_word_cache_is_invalidated_by_writers(self):
        async with self.sessions() as session:
            def check(text, mid):
                return auto_moderate(message(text, message_id=mid).as_(self.bot), self.bot, session)
            self.assertFalse(await check("forbidden", 1))  # loads and caches an empty list
            await cmd_addword(message("/addword forbidden", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertTrue(await check("forbidden", 2))
            await cmd_delword(message("/delword forbidden", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertFalse(await check("forbidden", 3))
            await add_banned_word(session, None, "forbidden")  # global word
            self.assertTrue(await check("forbidden", 4))


class CommandTests(DatabaseCase):
    async def test_addword_rejects_invisible_only_word(self):
        async with self.sessions() as session:
            await cmd_addword(message(f"/addword {ZW}{SHY}", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIn("невидимых", self.transport.requests("sendMessage")[-1].text)
            await cmd_addword(message("/addword one\ntwo", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIn("одной строке", self.transport.requests("sendMessage")[-1].text)

    async def test_domain_commands(self):
        async with self.sessions() as session:
            chat = await get_or_create_chat(session, CHAT_ID)
            await cmd_allowdomain(message("/allowdomain https://www.YouTube.com/", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertEqual(chat.allowed_domains, "youtube.com")
            await cmd_allowdomain(message("/allowdomain com", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertEqual(chat.allowed_domains, "youtube.com")
            reply = self.transport.requests("sendMessage")[-1].text
            self.assertIn("Использование", reply)
            self.assertIn("Пропущено", reply)  # every argument invalid: skipped list is shown
            self.assertIn("com", reply)
            await cmd_denydomain(message("/denydomain youtube.com", user=ADMIN).as_(self.bot), self.bot, session)
            self.assertIsNone(chat.allowed_domains)

    async def test_domain_commands_require_admin(self):
        async with self.sessions() as session:
            for handler, text in ((cmd_allowdomain, "/allowdomain a.com"), (cmd_denydomain, "/denydomain a.com")):
                await handler(message(text, user=USER).as_(self.bot), self.bot, session)
                self.assertIn("⛔", self.transport.requests("sendMessage")[-1].text)
            chat = await get_or_create_chat(session, CHAT_ID)
            self.assertIsNone(chat.allowed_domains)
