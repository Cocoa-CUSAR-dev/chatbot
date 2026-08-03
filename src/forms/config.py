from pydantic_settings import BaseSettings, SettingsConfigDict


class FormsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    KOTLIN_BACKEND_URL: str


forms_settings = FormsConfig()  # type: ignore[call-arg]
