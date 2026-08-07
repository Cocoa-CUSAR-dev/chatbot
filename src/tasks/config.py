from pydantic_settings import BaseSettings, SettingsConfigDict


class TasksConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    GO_BACKEND_URL: str
    # Must match mobile-backend's CHATBOT_SERVICE_KEY -- see that repo's
    # .env.sample and internal/middleware/service_auth_middleware.go.
    GO_SERVICE_KEY: str


tasks_settings = TasksConfig()  # type: ignore[call-arg]
