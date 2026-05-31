import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent
from redis.asyncio import Redis

from config import get_settings
from bot.database.crud import add_banned_word, get_banned_words
from bot.database.engine import create_engine, create_sessionmaker
from bot.handlers import setup_routers
from bot.middlewares.antiflood import AntiFloodMiddleware
from bot.middlewares.db import DbSessionMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("kirians_filter")


async def seed_global_badwords(sessionmaker) -> None:
    """Load every data/badwords/*.txt file into global banned words once.

    One file per language; lines starting with '#' or blank are skipped.
    Words already present (from a previous run or another file) are not
    re-inserted, so this is safe to run on every startup.
    """
    badwords_dir = Path(__file__).parent / "data" / "badwords"
    files = sorted(badwords_dir.glob("*.txt")) if badwords_dir.is_dir() else []
    if not files:
        return
    async with sessionmaker() as session:
        existing = {w.pattern for w in await get_banned_words(session, 0)}
        added = 0
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                word = line.strip()
                if word and not word.startswith("#") and word not in existing:
                    await add_banned_word(session, None, word)
                    existing.add(word)
                    added += 1
        if added:
            logger.info("Seeded %d global banned words from %d file(s)", added, len(files))


async def main() -> None:
    settings = get_settings()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    # Schema is managed by Alembic (`alembic upgrade head`), run before the bot
    # starts — see the `command` in docker-compose.yml.
    engine = create_engine()
    sessionmaker = create_sessionmaker(engine)
    await seed_global_badwords(sessionmaker)

    dp = Dispatcher()
    dp["redis"] = redis  # injected into handlers that request `redis`

    # DB session for every update (messages + callbacks); antiflood for messages only.
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.message.middleware(AntiFloodMiddleware(redis))

    dp.include_router(setup_routers())

    @dp.errors()
    async def on_unhandled_error(event: ErrorEvent) -> bool:
        """Last-resort net: log any error a handler let through (e.g. a
        Telegram API rejection) instead of dumping a traceback, and keep the
        bot polling. Per-action errors are handled in their commands."""
        logger.warning("Unhandled update error: %s", event.exception)
        return True

    logger.info("Starting Kirians Filter bot")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await redis.aclose()
        await engine.dispose()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
