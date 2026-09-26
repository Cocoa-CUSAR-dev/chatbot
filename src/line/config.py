from pydantic_settings import BaseSettings, SettingsConfigDict


class LineConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LINE_CHANNEL_SECRET: str
    LINE_CHANNEL_ACCESS_TOKEN: str
    LIFF_ID: str = ""
    # US2-6 (docs-and-plan#133): base URL for the diary card's "view full
    # history" button. src/sso/client.py mints a short-lived token appended
    # as ?token=... so the link opens web-app already logged in (see
    # _generate_and_push_diary in router.py) -- this is a separate,
    # lightweight token-mint/exchange flow, not the LIFF-based
    # identity-linking already owned by mobile-backend (ADR 0002).
    WEB_APP_URL: str = ""


line_settings = LineConfig()  # type: ignore[call-arg]
