from src.llm.config import llm_settings


def test_llm_settings_defaults() -> None:
    # Both have real defaults (unlike forms/tasks/line settings), so LLMConfig
    # can construct even with zero LLM_* env vars set -- ADR 0004's "swap the
    # provider via config, not code" only matters once someone actually sets these.
    assert llm_settings.LLM_MODEL
    assert isinstance(llm_settings.LLM_API_KEY, str)
