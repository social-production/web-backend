from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost/social_production"
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 60
    message_encryption_key: str = "change-me-too"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "*"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
