from pydantic_settings import BaseSettings, SettingsConfigDict


class LineConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LINE_CHANNEL_SECRET: str
    LINE_CHANNEL_ACCESS_TOKEN: str
    LIFF_ID: str = ""


line_settings = LineConfig()  # type: ignore[call-arg]
