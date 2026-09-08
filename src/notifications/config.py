from pydantic_settings import BaseSettings, SettingsConfigDict


class NotificationsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # First-party callers (web-backend, an admin tool) must present this as the
    # X-Service-Key header on POST /service/notifications. This is an INBOUND
    # key -- distinct from GO_SERVICE_KEY / KOTLIN_SERVICE_KEY, which are the
    # outbound keys this service presents when it calls those backends.
    CHATBOT_SERVICE_KEY: str


notifications_settings = NotificationsConfig()  # type: ignore[call-arg]
