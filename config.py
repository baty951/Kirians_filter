from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = Field(alias="BOT_TOKEN")

    postgres_host: str = Field("postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, alias="POSTGRES_PORT")
    postgres_user: str = Field("kirians", alias="POSTGRES_USER")
    postgres_password: str = Field("changeme", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field("kirians_filter", alias="POSTGRES_DB")

    redis_host: str = Field("redis", alias="REDIS_HOST")
    redis_port: int = Field(6379, alias="REDIS_PORT")
    redis_db: int = Field(0, alias="REDIS_DB")

    default_warn_limit: int = Field(3, alias="DEFAULT_WARN_LIMIT")
    default_mute_minutes: int = Field(60, alias="DEFAULT_MUTE_MINUTES")
    antiflood_messages: int = Field(5, alias="ANTIFLOOD_MESSAGES")
    antiflood_window_seconds: int = Field(5, alias="ANTIFLOOD_WINDOW_SECONDS")
    captcha_timeout_seconds: int = Field(120, alias="CAPTCHA_TIMEOUT_SECONDS")

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
