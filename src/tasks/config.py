from pydantic_settings import BaseSettings, SettingsConfigDict


class TasksConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    GO_BACKEND_URL: str


tasks_settings = TasksConfig()  # type: ignore[call-arg]
