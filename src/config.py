from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"

    @property
    def is_deployed(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: Environment = Environment.LOCAL

    DATABASE_URL: str

    KOTLIN_BACKEND_URL: str
    GO_BACKEND_URL: str

    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    APP_VERSION: str = "0.1.0"

    # Error tracking (X-2d). Blank DSN (the default) disables the SDK
    # entirely -- safe to leave unset in local dev/CI. No separate
    # SENTRY_ENVIRONMENT setting -- main.py passes ENVIRONMENT.value
    # straight through, so there's only one "what environment is this"
    # knob to keep in sync instead of two that can silently drift apart.
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0


settings = Settings()  # type: ignore[call-arg]
