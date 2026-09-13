from pydantic_settings import BaseSettings, SettingsConfigDict


class InternalConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Gates POST/GET /internal/cron/* -- whoever triggers a job externally
    # (Vercel Cron, a GitHub Actions schedule, a manual curl) must present
    # this as `Authorization: Bearer <CRON_SECRET>`. Vercel itself sends
    # exactly that header automatically for its own Cron Jobs once this env
    # var is set on the project (see Vercel's Cron Jobs docs) -- so the same
    # value doubles as both "the secret this service checks" and "the value
    # Vercel already knows to send," no extra wiring needed on that side.
    CRON_SECRET: str


internal_settings = InternalConfig()  # type: ignore[call-arg]
