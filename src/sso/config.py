from pydantic_settings import BaseSettings, SettingsConfigDict


class SsoConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Same Kotlin backend src/diary/config.py talks to -- kept as this
    # domain's own config rather than importing src.diary.config, matching
    # the one-config-per-domain convention every other capability folder
    # here already follows.
    KOTLIN_BACKEND_URL: str
    # Must match web-backend's CHATBOT_SERVICE_KEY -- see that repo's
    # .env.sample and ServiceKeyFilter.
    KOTLIN_SERVICE_KEY: str


sso_settings = SsoConfig()  # type: ignore[call-arg]
