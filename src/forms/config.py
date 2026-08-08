from pydantic_settings import BaseSettings, SettingsConfigDict


class FormsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    KOTLIN_BACKEND_URL: str
    # Must match web-backend's CHATBOT_SERVICE_KEY -- see that repo's
    # .env.sample and ServiceKeyFilter.
    KOTLIN_SERVICE_KEY: str


forms_settings = FormsConfig()  # type: ignore[call-arg]
