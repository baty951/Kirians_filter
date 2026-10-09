from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = Field(alias="BOT_TOKEN")

    # Comma-separated Telegram user IDs immune to all restrictions.
    protected_ids_raw: str = Field("", alias="PROTECTED_IDS")

    postgres_host: str = Field("postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, alias="POSTGRES_PORT")
    postgres_user: str = Field("kirians", alias="POSTGRES_USER")
    postgres_password: str = Field("changeme", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field("kirians_filter", alias="POSTGRES_DB")

    redis_host: str = Field("redis", alias="REDIS_HOST")
    redis_port: int = Field(6379, alias="REDIS_PORT")
    redis_db: int = Field(0, alias="REDIS_DB")

    default_warn_limit: int = Field(3, alias="DEFAULT_WARN_LIMIT", ge=1)
    default_mute_minutes: int = Field(60, alias="DEFAULT_MUTE_MINUTES", ge=1, le=527039)
    antiflood_messages: int = Field(5, alias="ANTIFLOOD_MESSAGES", ge=1)
    antiflood_window_seconds: int = Field(5, alias="ANTIFLOOD_WINDOW_SECONDS", ge=1)
    captcha_timeout_seconds: int = Field(120, alias="CAPTCHA_TIMEOUT_SECONDS", ge=1)
    archive_retention_days: int = Field(90, alias="ARCHIVE_RETENTION_DAYS", ge=1)

    @property
    def protected_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.protected_ids_raw.split(","):
            part = part.strip()
            if part.isdecimal():
                ids.add(int(part))
        return ids

    @property
    def postgres_dsn(self) -> str:
        return URL.create(
            "postgresql+asyncpg", username=self.postgres_user,
            password=self.postgres_password, host=self.postgres_host,
            port=self.postgres_port, database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
