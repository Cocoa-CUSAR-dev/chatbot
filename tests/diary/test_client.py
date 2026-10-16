"""src/diary/client.py: the Kotlin call that writes the farmer's diary after
a confirmed submission (docs-and-plan#247). The router treats every failure
here as "no diary card, submission still saved", so each one has to surface
as an exception the router can catch, never a half-read response.
"""

import json

import pytest
import respx
from httpx import Response

from src.diary.client import generate_diary
from src.diary.config import diary_settings
from src.diary.exceptions import DiaryNotAvailable
from src.exceptions import UpstreamServiceError

_URL = f"{diary_settings.KOTLIN_BACKEND_URL}/service/diaries/generate"


@respx.mock
async def test_returns_the_diary_text_and_sends_the_service_key() -> None:
    route = respx.post(_URL).mock(
        return_value=Response(200, json={"value": "วันนี้ใส่ปุ๋ย", "error": None})
    )

    assert await generate_diary("u-1") == "วันนี้ใส่ปุ๋ย"
    request = route.calls.last.request
    assert json.loads(request.content) == {"userId": "u-1"}
    assert request.headers["X-Service-Key"] == diary_settings.KOTLIN_SERVICE_KEY


@respx.mock
async def test_404_means_no_diary_for_this_user() -> None:
    respx.post(_URL).mock(return_value=Response(404, json={"error": "nothing today"}))
    with pytest.raises(DiaryNotAvailable):
        await generate_diary("u-1")


@respx.mock
async def test_401_names_the_service_key_mismatch() -> None:
    respx.post(_URL).mock(return_value=Response(401))
    with pytest.raises(UpstreamServiceError, match="KOTLIN_SERVICE_KEY"):
        await generate_diary("u-1")


@respx.mock
async def test_other_errors_raise() -> None:
    respx.post(_URL).mock(return_value=Response(502))
    with pytest.raises(UpstreamServiceError, match="502"):
        await generate_diary("u-1")


@respx.mock
async def test_a_200_carrying_an_error_envelope_raises() -> None:
    respx.post(_URL).mock(return_value=Response(200, json={"value": None, "error": "LLM down"}))
    with pytest.raises(UpstreamServiceError, match="LLM down"):
        await generate_diary("u-1")
