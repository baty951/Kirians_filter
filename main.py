import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import gettempdir

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent
from redis.asyncio import Redis

from config import get_settings
from bot.database.crud import add_banned_word, get_banned_words, purge_archives
from bot.database.engine import create_engine, create_sessionmaker
from bot.handlers import setup_routers
from bot.handlers.welcome import expire_captchas
from bot.middlewares.antiflood import AntiFloodMiddleware
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.message_store import MessageStoreMiddleware
from bot.middlewares.moderation import ModerationMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("kirians_filter")
READY_FILE = Path(gettempdir()) / "kirians-filter.ready"
ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post", "callback_query", "chat_member"]


def create_dispatcher(redis: Redis, sessions) -> Dispatcher:
    dp = Dispatcher()
    dp["redis"] = redis
    dp.update.outer_middleware(DbSessionMiddleware(sessions))
    for update_type in ("message", "edited_message", "channel_post", "edited_channel_post"):
        observer = dp.observers[update_type]
        observer.outer_middleware(MessageStoreMiddleware())
        if update_type in ("message", "channel_post"):
            observer.outer_middleware(AntiFloodMiddleware(redis))
        observer.outer_middleware(ModerationMiddleware())
    dp.include_router(setup_routers())

    @dp.errors()
    async def on_unhandled_error(event: ErrorEvent) -> bool:
        logger.error("Unhandled update error", exc_info=(type(event.exception), event.exception, event.exception.__traceback__))
        return True

    return dp


async def maintain_storage(bot: Bot, sessions) -> None:
    next_cleanup = 0.0
    while True:
        try:
            await expire_captchas(bot, sessions)
        except Exception:
            logger.exception("Captcha expiry failed; retrying")
        if asyncio.get_running_loop().time() >= next_cleanup:
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().archive_retention_days)
                async with sessions() as session:
                    await purge_archives(session, cutoff)
                next_cleanup = asyncio.get_running_loop().time() + 3600
            except Exception:
                logger.exception("Archive cleanup failed; retrying")
        await asyncio.sleep(5)


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
    redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)

    # Schema is managed by Alembic (`alembic upgrade head`), run before the bot
    # starts — see the `command` in docker-compose.yml.
    engine = create_engine()
    sessionmaker = create_sessionmaker(engine)
    maintenance = None
    READY_FILE.unlink(missing_ok=True)
    try:
        await seed_global_badwords(sessionmaker)
        dp = create_dispatcher(redis, sessionmaker)
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.get_me()
        maintenance = asyncio.create_task(maintain_storage(bot, sessionmaker))
        READY_FILE.parent.mkdir(parents=True, exist_ok=True)
        READY_FILE.touch()
        logger.info("Starting Kirians Filter bot")
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, close_bot_session=False)
    finally:
        READY_FILE.unlink(missing_ok=True)
        if maintenance is not None:
            maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance
        await redis.aclose()
        await engine.dispose()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
