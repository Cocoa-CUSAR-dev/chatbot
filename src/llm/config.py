from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LiteLLM-format model string -- e.g. "gpt-4o-mini", "claude-3-5-haiku-20241022",
    # "gemini/gemini-1.5-flash". Switching providers is this one string, not code
    # (ADR 0004).
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_API_KEY: str = ""
    # A farmer is waiting in LINE for the reply, so a slow provider is treated
    # the same as an unavailable one (LLMUnavailable) rather than waited on.
    LLM_TIMEOUT_SECONDS: float = 6.0


llm_settings = LLMConfig()
