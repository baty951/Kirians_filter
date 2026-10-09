import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:ABC-test-token")

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from bot.database.crud import add_banned_word, add_log, add_warning, get_or_create_chat
from bot.database.models import Base, Chat
from bot.middlewares.antiflood import _SLIDING_WINDOW
from bot.utils.helpers import extract_target_and_reason, parse_duration
from config import Settings

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_malformed_user_id_is_rejected_without_action(self):
        from support import message
        for target in ("--42", "-42", "0", "not-an-id"):
            with self.subTest(target=target):
                self.assertEqual(extract_target_and_reason(message(f"/mute {target} 2h")), (None, None))
        self.assertEqual(extract_target_and_reason(message("/mute 42 2h")), (42, "2h"))

    def test_duration_bounds_and_explicit_permanent_option(self):
        for value in ("0s", "10s", "0m", "366d", "400d", "999999999999999999999d"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_duration(value)
        self.assertEqual(parse_duration("60s").total_seconds(), 60)
        self.assertEqual(parse_duration("365d").days, 365)
        self.assertIsNone(parse_duration(None))
        self.assertIsNone(parse_duration("reason"))

    def test_passwords_and_usernames_round_trip(self):
        settings = Settings(BOT_TOKEN="123456:ABC-test-token", POSTGRES_USER="user@name",
                            POSTGRES_PASSWORD="test@:%/word", _env_file=None)
        url = make_url(settings.postgres_dsn)
        self.assertEqual(url.username, "user@name")
        self.assertEqual(url.password, "test@:%/word")

    def test_alembic_offline_with_percent_password(self):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
                                cwd=ROOT, env=os.environ | {"POSTGRES_PASSWORD": "fake@:%/word"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CREATE TABLE captcha_challenges", result.stdout)

    def test_invalid_settings_are_rejected(self):
        for key, value in (("ANTIFLOOD_MESSAGES", 0), ("ARCHIVE_RETENTION_DAYS", 0),
                           ("DEFAULT_MUTE_MINUTES", 527040), ("CAPTCHA_TIMEOUT_SECONDS", -1)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Settings(BOT_TOKEN="123456:ABC-test-token", _env_file=None, **{key: value})


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_restart_preserves_updates_and_closes_resources(self):
        import main
        bot = SimpleNamespace(delete_webhook=AsyncMock(), get_me=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        redis = SimpleNamespace(aclose=AsyncMock())
        engine = SimpleNamespace(dispose=AsyncMock())
        dispatcher = SimpleNamespace(start_polling=AsyncMock())
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(main, "READY_FILE", Path(temp) / "ready"), \
                 patch.object(main, "Bot", return_value=bot), \
                 patch.object(main.Redis, "from_url", return_value=redis), \
                 patch.object(main, "create_engine", return_value=engine), \
                 patch.object(main, "create_sessionmaker"), \
                 patch.object(main, "seed_global_badwords", new_callable=AsyncMock), \
                 patch.object(main, "create_dispatcher", return_value=dispatcher), \
                 patch.object(main, "maintain_storage", new_callable=AsyncMock):
                await main.main()
            self.assertFalse((Path(temp) / "ready").exists())
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        self.assertEqual(dispatcher.start_polling.call_args.kwargs["allowed_updates"], main.ALLOWED_UPDATES)
        bot.session.close.assert_awaited_once()
        redis.aclose.assert_awaited_once()
        engine.dispose.assert_awaited_once()


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_DSN"), "Set TEST_POSTGRES_DSN for PostgreSQL integration checks")
class PostgreSQLTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "test_" + uuid.uuid4().hex
        self.engine = create_async_engine(os.environ["TEST_POSTGRES_DSN"],
                                          execution_options={"schema_translate_map": {None: self.schema}})
        async with self.engine.begin() as connection:
            await connection.execute(CreateSchema(self.schema))
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True))
        await self.engine.dispose()

    async def test_concurrent_first_chat_creation_and_foreign_keys(self):
        async def create_chat():
            async with self.sessions() as session:
                return (await get_or_create_chat(session, -10042)).chat_id
        self.assertEqual(await asyncio.gather(*(create_chat() for _ in range(12))), [-10042] * 12)
        async with self.sessions() as session:
            await add_log(session, -10043, "MUTE", reason="reason")
            await add_warning(session, -10044, 42, 99, "reason")
            await add_banned_word(session, -10045, "word")
            self.assertEqual(await session.scalar(select(func.count()).select_from(Chat)), 4)


@unittest.skipUnless(os.environ.get("TEST_REDIS_URL"), "Set TEST_REDIS_URL for Redis Lua integration checks")
class RedisTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_sliding_window_and_expiry(self):
        from redis.asyncio import Redis
        redis = Redis.from_url(os.environ["TEST_REDIS_URL"])
        key = "test:flood:" + uuid.uuid4().hex
        try:
            # Seed old traffic on either side of the window boundary using server time.
            seconds, microseconds = await redis.time()
            now = seconds * 1000 + microseconds // 1000
            await redis.zadd(key, {"expired": now - 60000, "recent": now - 100})
            self.assertEqual(await redis.eval(_SLIDING_WINDOW, 1, key, 1000, 5, "current"), 2)
            self.assertEqual(await redis.eval(_SLIDING_WINDOW, 1, key, 1000, 5, "current"), 2)
            counts = await asyncio.gather(*(redis.eval(_SLIDING_WINDOW, 1, key, 1000, 5, str(i)) for i in range(20)))
            self.assertEqual(max(counts), 6)
            self.assertEqual(await redis.zcard(key), 5)
            self.assertGreater(await redis.pttl(key), 0)
            self.assertLessEqual(await redis.pttl(key), 1000)
            await asyncio.sleep(1.1)
            self.assertFalse(await redis.exists(key))
        finally:
            await redis.delete(key)
            await redis.aclose()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.bash = (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash"))
        if not self.bash or not Path(self.bash).exists():
            self.skipTest("Bash is required to check deploy.sh")

    def run_deploy(self, volumes="", explicit="", env_project="", fail_up=False):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / ".env").write_text(explicit, encoding="utf-8")
            docker = folder / "docker"
            docker.write_text('''#!/usr/bin/env bash
printf '%s|%s\\n' "${COMPOSE_PROJECT_NAME:-kirians_filter}" "$*" >> "$DEPLOY_TEST_LOG"
if [[ "$1 $2" == "volume inspect" ]]; then
    [[ ",${AVAILABLE_VOLUMES}," == *",$3,"* ]]
elif [[ "$1 $2" == "compose up" && "${FAIL_UP:-0}" == 1 ]]; then
    exit 17
fi
''', encoding="utf-8")
            docker.chmod(0o755)
            log = folder / "calls.log"
            env = os.environ | {"PATH": str(folder) + os.pathsep + os.environ["PATH"],
                                "DEPLOY_TEST_LOG": log.as_posix(), "AVAILABLE_VOLUMES": volumes,
                                "COMPOSE_PROJECT_NAME": env_project, "FAIL_UP": "1" if fail_up else "0"}
            result = subprocess.run([self.bash, (ROOT / "scripts" / "deploy.sh").as_posix()],
                                    cwd=folder, env=env, capture_output=True, text=True, timeout=30)
            result.env_text = (folder / ".env").read_text()
            return result, log.read_text() if log.exists() else ""

    def test_fresh_and_legacy_installations_select_expected_project(self):
        for volumes, expected in (("", "kirians_filter"), ("kirians_filter_pgdata", "kirians_filter"),
                                  ("kirians-filter_pgdata", "kirians-filter")):
            with self.subTest(volumes=volumes):
                result, log = self.run_deploy(volumes)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected + "|compose up", log)
                self.assertIn("--wait --wait-timeout", log)
                if expected == "kirians-filter":
                    self.assertIn("COMPOSE_PROJECT_NAME=kirians-filter", result.env_text)

    def test_ambiguous_volumes_abort_before_starting_containers(self):
        result, log = self.run_deploy("kirians_filter_pgdata,kirians-filter_pgdata")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set COMPOSE_PROJECT_NAME", result.stderr)
        self.assertNotIn("compose up", log)

    def test_explicit_project_selection_overrides_detection(self):
        result, log = self.run_deploy("kirians_filter_pgdata,kirians-filter_pgdata", env_project="chosen")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("chosen|compose up", log)
        self.assertNotIn("volume inspect", log)
        result, log = self.run_deploy("kirians_filter_pgdata,kirians-filter_pgdata", explicit="COMPOSE_PROJECT_NAME=kirians-filter\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("volume inspect", log)

    def test_failed_startup_does_not_prune_images(self):
        result, log = self.run_deploy(fail_up=True)
        self.assertEqual(result.returncode, 17)
        self.assertNotIn("image prune", log)
