from pydantic_settings import BaseSettings, SettingsConfigDict


class LineConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LINE_CHANNEL_SECRET: str
    LINE_CHANNEL_ACCESS_TOKEN: str
    LIFF_ID: str = ""
    # US2-6 (docs-and-plan#133): the diary card's "view full history" button
    # links here plainly for now -- US3-2 (real SSO between LINE OA and the
    # web platform) is still Todo, not something this feature can build on
    # yet, so there's no token to attach. Update this link once US3-2 lands
    # instead of reviving a half-built SSO attempt here.
    WEB_APP_URL: str = ""


line_settings = LineConfig()  # type: ignore[call-arg]
