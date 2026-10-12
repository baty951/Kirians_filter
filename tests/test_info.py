from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from bot.database.crud import add_log, get_or_create_chat
from bot.database.models import ActionLog
from bot.handlers.info import cmd_modlog, cmd_rules, cmd_setrules, cmd_stats
from support import ADMIN, CHAT_ID, USER, DatabaseCase, message


class StatsTests(DatabaseCase):
    async def log(self, session, action, target=USER.id, reason=None, days_ago=0):
        await add_log(session, CHAT_ID, action, actor_id=ADMIN.id, target_id=target, reason=reason, content=None)
        if days_ago:
            row = (await session.scalars(select(ActionLog).order_by(ActionLog.id.desc()).limit(1))).one()
            row.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
            await session.commit()

    def reply(self):
        return self.transport.requests("sendMessage")[-1].text

    async def ask(self, session, handler, text, user=ADMIN):
        await handler(message(text, user=user).as_(self.bot), self.bot, session)
        return self.reply()

    async def test_stats_counts_window_and_top_offenders(self):
        async with self.sessions() as session:
            for _ in range(3):
                await self.log(session, "DELETE", target=USER.id)
            await self.log(session, "DELETE", target=77)
            await self.log(session, "BAN", target=88)  # a manual ban is counted but is not an "offense"
            await self.log(session, "DELETE", target=USER.id, days_ago=10)  # outside the default 7 days
            text = await self.ask(session, cmd_stats, "/stats")
            self.assertIn("за 7 дн.", text)
            self.assertIn("Удалено сообщений: 4", text)
            self.assertIn("Банов: 1", text)
            self.assertLess(text.index(f"id={USER.id}"), text.index("id=77"))
            self.assertNotIn("id=88", text)
            self.assertIn("Удалено сообщений: 5", await self.ask(session, cmd_stats, "/stats 30"))

    async def test_stats_arguments_and_permissions(self):
        async with self.sessions() as session:
            self.assertIn("Использование", await self.ask(session, cmd_stats, "/stats 0"))
            self.assertIn("Использование", await self.ask(session, cmd_stats, "/stats 91"))
            self.assertIn("Использование", await self.ask(session, cmd_stats, "/stats abc"))
            self.assertIn("нет", await self.ask(session, cmd_stats, "/stats"))
            self.assertIn("⛔", await self.ask(session, cmd_stats, "/stats", user=USER))

    async def test_modlog_shows_a_users_history_escaped(self):
        async with self.sessions() as session:
            await self.log(session, "WARN", reason="<b>spam</b>")
            await self.log(session, "MUTE", target=77)
            text = await self.ask(session, cmd_modlog, f"/modlog {USER.id}")
            self.assertIn("WARN", text)
            self.assertIn("&lt;b&gt;spam&lt;/b&gt;", text)
            self.assertNotIn("MUTE", text)
            self.assertIn("записей нет", await self.ask(session, cmd_modlog, "/modlog 5"))
            self.assertIn("Ответьте", await self.ask(session, cmd_modlog, "/modlog"))
            self.assertIn("⛔", await self.ask(session, cmd_modlog, f"/modlog {USER.id}", user=USER))


class RulesTests(DatabaseCase):
    async def test_rules_roundtrip_escape_and_limits(self):
        async with self.sessions() as session:
            async def say(text, handler=cmd_setrules, user=ADMIN):
                event = message(text, user=user).as_(self.bot)
                await (handler(event, session) if handler is cmd_rules else handler(event, self.bot, session))
                return self.transport.requests("sendMessage")[-1].text

            self.assertIn("не заданы", await say("/rules", cmd_rules, USER))
            self.assertIn("⛔", await say("/setrules be nice", user=USER))
            self.assertIn("обновлены", await say("/setrules 1. <b>No</b> spam"))
            shown = await say("/rules", cmd_rules, USER)  # anyone can read them
            self.assertIn("1. &lt;b&gt;No&lt;/b&gt; spam", shown)
            self.assertIn("слишком длинные", await say("/setrules " + "x" * 1501))
            self.assertEqual((await get_or_create_chat(session, CHAT_ID)).rules_text, "1. &lt;b&gt;No&lt;/b&gt; spam")
            self.assertIn("сброшены", await say("/setrules"))
            self.assertIn("не заданы", await say("/rules", cmd_rules, USER))
