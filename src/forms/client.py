"""Form Proxy Client -- read-only. Calls Kotlin's EXISTING GET /forms/{formId}
rather than duplicating its form-assembly logic (ADR 0001: reuse, don't
rebuild -- avoids repeating GO-1's split-brain-data-access mistake a third
time).

Kotlin's real response shape (confirmed by reading web-backend's
FormController/FormService/FormRepository directly -- never actually
exercised against a live Kotlin instance before this): every response is
wrapped as `{"value": {...}, "error": null}` (web-backend's `ApiResponse<T>`,
same envelope Go's own GetTaskForm proxy already unwraps), and Kotlin's
Jackson serialization uses camelCase (`fieldName`, `isMandatory`,
`questionId`, `formId`) since nothing configures otherwise. Neither of these
was handled here before -- get_form() would have failed validation outright
on the envelope, and even past that, every question would have silently come
through as not-mandatory with no field_name. get_form() now unwraps and
converts both before handing off to FormDetail.
"""

import re
from typing import Any

import httpx

from src.exceptions import UpstreamServiceError
from src.forms.config import forms_settings
from src.forms.exceptions import FormNotFound
from src.forms.schemas import FormDetail

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _camel_to_snake(key: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", key).lower()


def _convert_keys(value: Any) -> Any:
    """Recursively converts camelCase dict keys to snake_case -- needed for
    the nested sections[].questions[] structure, not just the top level.
    """
    if isinstance(value, dict):
        return {_camel_to_snake(k): _convert_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert_keys(item) for item in value]
    return value


async def get_form(form_id: str) -> FormDetail:
    async with httpx.AsyncClient(base_url=forms_settings.KOTLIN_BACKEND_URL) as client:
        response = await client.get(f"/forms/{form_id}")

    if response.status_code == 404:
        raise FormNotFound(f"Form {form_id} not found")
    if response.status_code >= 400:
        raise UpstreamServiceError(f"Kotlin backend returned {response.status_code}")

    body = response.json()
    error = body.get("error")
    if error:
        raise UpstreamServiceError(f"Kotlin returned an error envelope: {error}")

    value = _convert_keys(body.get("value", {}))
    # Kotlin's field is `formId` (form.task_form.form_id) -- everywhere else
    # in this repo calls the same thing task_form_id (matches
    # chat.conversation.task_form_id), so rename it here once rather than at
    # every call site.
    if "form_id" in value:
        value["task_form_id"] = value.pop("form_id")

    return FormDetail.model_validate(value)
