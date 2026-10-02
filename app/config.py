import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# SHA-256 of Fernet keys that were published and must never be reused.
_REVOKED_MESSAGE_KEY_DIGESTS = frozenset(
    {
        "37ce0034ed3fccd87ec501d53e6e6117c62d05f0c7de824b6e8c6f681b77e283",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost/social_production"
    jwt_secret: str = "dev-only-change-me"
    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 30
    rate_limit_fail_closed: bool = False
    message_encryption_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout_seconds: float = 2.0
    redis_socket_connect_timeout_seconds: float = 2.0
    redis_max_connections: int = 50
    redis_cache_ttl_seconds: int = 3600
    cors_origins: str = "http://localhost:5173"
    github_token: str = ""
    github_repo: str = "social-production/web"
    disable_openapi_in_production: bool = True
    # OSM-compatible geocoding provider (Nominatim-style). Credentials stay server-side.
    geocoding_provider_url: str = "https://nominatim.openstreetmap.org"
    geocoding_provider_api_key: str = ""
    geocoding_user_agent: str = "SocialProduction/0.1 (location-foundation)"
    geocoding_cache_ttl_seconds: int = 86400
    geocoding_rate_limit: int = 30
    geocoding_rate_limit_window_seconds: int = 60
    geocoding_timeout_seconds: float = 8.0
    # Instant switch: set SIGNUP_ENABLED=false and restart to refuse new accounts.
    signup_enabled: bool = True
    # Empty skips captcha. Set TURNSTILE_SECRET_KEY to require Cloudflare Turnstile on signup.
    turnstile_secret_key: str = ""
    # Governance warm-up. 0 leaves voting unchanged. Set both to require age and activity
    # before an account can vote, count toward quorum, or volunteer as a moderator.
    governance_min_account_age_hours: int = 0
    governance_min_meaningful_actions: int = 0
    governance_meaningful_action_types: str = (
        "create-comment,create-post,create-thread,create-help-request,"
        "create-project,create-event,submit-project-plan,submit-event-plan,"
        "create-platform-feedback"
    )
    # Hours added to the warm-up start when the account's content is removed or hidden.
    governance_removal_penalty_hours: int = 0
    # Sybil attack. The ratio switch is off until this is true. Restart the API after changes.
    # real_r is display-only. Stances, votes, quorum, and activity bands use effective_r.
    governance_trust_ratio_enabled: bool = False
    governance_min_bot_marks: int = 3
    governance_mark_license_vouches: int = 5
    governance_vote_threshold: float = 0.66
    governance_limited_threshold: float = 0.30
    governance_inert_threshold: float = 0.10
    governance_bootstrap_markers: int = 10
    governance_bootstrap_target: int = 20

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

        self.rate_limit_fail_closed = True

        weak_jwt_secrets = {"change-me", "dev-only-change-me", ""}
        weak_message_keys = {"change-me-too", "dev-only-change-me-too", ""}
        message_key = self.message_encryption_key.strip()
        message_key_digest = hashlib.sha256(message_key.encode("utf-8")).hexdigest()
        if self.jwt_secret.strip() in weak_jwt_secrets:
            raise RuntimeError("JWT_SECRET must be set to a strong value in production")
        if message_key in weak_message_keys or message_key_digest in _REVOKED_MESSAGE_KEY_DIGESTS:
            raise RuntimeError("MESSAGE_ENCRYPTION_KEY must be set to a Fernet key in production")
        try:
            Fernet(message_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "MESSAGE_ENCRYPTION_KEY must be set to a Fernet key in production"
            ) from exc
        if "*" in self.cors_origin_list:
            raise RuntimeError("CORS_ORIGINS must list explicit origins in production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
