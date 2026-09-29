from unittest.mock import AsyncMock, patch

from src.conversation import llm_parsing
from src.llm.exceptions import LLMUnavailable


def _with_key(key: str = "test-key"):  # noqa: ANN202
    return patch.object(llm_parsing.llm_settings, "LLM_API_KEY", key)


def _extract(value: str | None) -> AsyncMock:
    return AsyncMock(return_value=llm_parsing._ExtractedValue(value=value))


async def test_returns_the_models_value() -> None:
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=_extract("900")):
        assert await llm_parsing.try_llm_parse("INT", "เก้าร้อยกว่าๆ") == "900"


async def test_value_is_stripped() -> None:
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=_extract(" 12.5 ")):
        assert await llm_parsing.try_llm_parse("FLOAT", "สิบสองครึ่ง") == "12.5"


async def test_null_answer_returns_none() -> None:
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=_extract(None)):
        assert await llm_parsing.try_llm_parse("INT", "กากๆ") is None


async def test_blank_answer_returns_none() -> None:
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=_extract("  ")):
        assert await llm_parsing.try_llm_parse("INT", "กากๆ") is None


async def test_llm_unavailable_returns_none() -> None:
    mock = AsyncMock(side_effect=LLMUnavailable())
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=mock):
        assert await llm_parsing.try_llm_parse("INT", "ห้า") is None


async def test_malformed_model_response_returns_none() -> None:
    mock = AsyncMock(side_effect=ValueError("bad json"))
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=mock):
        assert await llm_parsing.try_llm_parse("INT", "ห้า") is None


async def test_no_api_key_skips_the_llm_entirely() -> None:
    mock = _extract("5")
    with _with_key(""), patch("src.conversation.llm_parsing.extract_slots", new=mock):
        assert await llm_parsing.try_llm_parse("INT", "ห้า") is None
    mock.assert_not_awaited()


async def test_unsupported_type_skips_the_llm() -> None:
    mock = _extract("x")
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=mock):
        assert await llm_parsing.try_llm_parse("VARCHAR", "text") is None
    mock.assert_not_awaited()


async def test_instructions_carry_the_type_and_todays_date() -> None:
    mock = _extract("2026-01-01")
    with _with_key(), patch("src.conversation.llm_parsing.extract_slots", new=mock):
        await llm_parsing.try_llm_parse("DATE", "ปีใหม่")
    instructions = mock.await_args.kwargs["instructions"]
    assert "YYYY-MM-DD" in instructions
    assert "Today is 20" in instructions
