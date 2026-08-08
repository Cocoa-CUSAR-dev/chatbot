from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.exceptions import UpstreamServiceError
from src.forms.client import _camel_to_snake, _convert_keys, get_form
from src.forms.exceptions import FormNotFound


def test_camel_to_snake_converts_common_cases() -> None:
    assert _camel_to_snake("fieldName") == "field_name"
    assert _camel_to_snake("isMandatory") == "is_mandatory"
    assert _camel_to_snake("questionId") == "question_id"
    assert _camel_to_snake("formId") == "form_id"
    assert _camel_to_snake("already_snake") == "already_snake"
    assert _camel_to_snake("id") == "id"


def test_convert_keys_recurses_into_nested_sections_and_questions() -> None:
    raw = {
        "formId": "f1",
        "sections": [
            {
                "sectionId": "s1",
                "questions": [
                    {"questionId": "q1", "isMandatory": True, "fieldName": "farm_id"},
                ],
            }
        ],
    }

    converted = _convert_keys(raw)

    assert converted["form_id"] == "f1"
    question = converted["sections"][0]["questions"][0]
    assert question["question_id"] == "q1"
    assert question["is_mandatory"] is True
    assert question["field_name"] == "farm_id"


def _mock_kotlin_response(status_code: int, body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return response


def _patched_client(response: MagicMock):
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.forms.client.httpx.AsyncClient", return_value=client)


async def test_get_form_unwraps_envelope_and_converts_keys() -> None:
    """The realistic case -- Kotlin's actual response shape (envelope +
    camelCase + `formId` not `task_form_id`), confirmed by reading
    web-backend's FormRepository/Question/Form DTOs directly.
    """
    kotlin_body = {
        "value": {
            "formId": "form-abc",
            "title": "จดกิจกรรมในสวน",
            "sections": [
                {
                    "sectionId": "sec-1",
                    "questions": [
                        {
                            "questionId": "q-1",
                            "label": "ฟาร์มที่ทำกิจกรรม",
                            "fieldName": "farm_id",
                            "isMandatory": True,
                            "sortOrder": 0,
                        }
                    ],
                }
            ],
        },
        "error": None,
    }
    response = _mock_kotlin_response(200, kotlin_body)

    with _patched_client(response):
        form = await get_form("form-abc")

    assert form.task_form_id == "form-abc"
    question = form.sections[0]["questions"][0]
    assert question["question_id"] == "q-1"
    assert question["field_name"] == "farm_id"
    assert question["is_mandatory"] is True
    assert question["sort_order"] == 0


async def test_get_form_raises_on_error_envelope() -> None:
    # HTTP 200 but Kotlin's own error field is set -- a real failure mode
    # distinct from an HTTP-level error, must not be treated as success.
    response = _mock_kotlin_response(200, {"value": None, "error": "form not found"})

    with _patched_client(response), pytest.raises(UpstreamServiceError):
        await get_form("missing-form")


async def test_get_form_404_raises_form_not_found() -> None:
    response = _mock_kotlin_response(404, {"error": "not found"})

    with _patched_client(response), pytest.raises(FormNotFound):
        await get_form("missing-form")
