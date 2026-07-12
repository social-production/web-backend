from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost/social_production"
    jwt_secret: str = "dev-only-change-me"
    jwt_expire_minutes: int = 60 * 24 * 7
    message_encryption_key: str = "IoR_TjHO_mc373uQePi0GDzCouould4_1Sx6TB4ChD8="
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout_seconds: float = 2.0
    redis_socket_connect_timeout_seconds: float = 2.0
    redis_max_connections: int = 50
    redis_cache_ttl_seconds: int = 3600
    cors_origins: str = "http://localhost:5173"
    github_token: str = ""
    github_repo: str = "social-production/web"
    disable_openapi_in_production: bool = True

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        url = (value or "").strip()
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        if url.startswith("postgresql://") and "+psycopg" not in url:
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allow_cors_credentials(self) -> bool:
        return "*" not in self.cors_origin_list

    def validate_runtime_settings(self) -> None:
        if not self.is_production:
            return

        weak_jwt_secrets = {"change-me", "dev-only-change-me", ""}
        weak_message_keys = {"change-me-too", "dev-only-change-me-too", "IoR_TjHO_mc373uQePi0GDzCouould4_1Sx6TB4ChD8=", ""}
        if self.jwt_secret.strip() in weak_jwt_secrets:
            raise RuntimeError("JWT_SECRET must be set to a strong value in production")
        if self.message_encryption_key.strip() in weak_message_keys:
            raise RuntimeError("MESSAGE_ENCRYPTION_KEY must be set to a Fernet key in production")
        if "*" in self.cors_origin_list:
            raise RuntimeError("CORS_ORIGINS must list explicit origins in production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
