from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from src.llm.client import extract_slots, generate_follow_up
from src.llm.exceptions import LLMUnavailable


class _FarmSlots(BaseModel):
    farm_name: str | None = None
    plot_id: str | None = None


def _mock_completion(content: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


async def test_extract_slots_parses_the_model_response_into_the_schema() -> None:
    response = _mock_completion('{"farm_name": "สวนทดสอบ", "plot_id": null}')
    with patch("src.llm.client.litellm.acompletion", new=AsyncMock(return_value=response)):
        result = await extract_slots("ฉันทำงานที่สวนทดสอบ", _FarmSlots)

    assert result.farm_name == "สวนทดสอบ"
    assert result.plot_id is None


async def test_extract_slots_raises_llm_unavailable_on_provider_failure() -> None:
    with (
        patch(
            "src.llm.client.litellm.acompletion",
            new=AsyncMock(side_effect=TimeoutError("provider timed out")),
        ),
        pytest.raises(LLMUnavailable),
    ):
        await extract_slots("ข้อความ", _FarmSlots)


async def test_generate_follow_up_returns_the_models_text() -> None:
    response = _mock_completion("คุณทำกิจกรรมที่แปลงไหนครับ?")
    with patch("src.llm.client.litellm.acompletion", new=AsyncMock(return_value=response)):
        question = await generate_follow_up("plot_id", context="ฉันใส่ปุ๋ยวันนี้")

    assert question == "คุณทำกิจกรรมที่แปลงไหนครับ?"


async def test_generate_follow_up_raises_llm_unavailable_on_provider_failure() -> None:
    with (
        patch(
            "src.llm.client.litellm.acompletion",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(LLMUnavailable),
    ):
        await generate_follow_up("plot_id", context="ฉันใส่ปุ๋ยวันนี้")
