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


settings = Settings()  # type: ignore[call-arg]
