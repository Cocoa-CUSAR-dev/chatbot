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

    # US2-11 kill switch: False makes intent.classify return UNKNOWN without
    # calling the provider at all, so free-text routing reverts to the old
    # fixed hint message -- flipped in Vercel's env, no deploy needed, if the
    # classifier misbehaves in production.
    INTENT_ROUTING_ENABLED: bool = True
    # Below this self-reported confidence the message is treated as UNKNOWN.
    # A wrong-but-confident route costs the farmer trust; the hint costs one
    # extra tap.
    INTENT_MIN_CONFIDENCE: float = 0.6


llm_settings = LLMConfig()
