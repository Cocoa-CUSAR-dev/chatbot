"""Form Proxy Client -- read-only. Calls Kotlin's EXISTING GET /forms/{formId}
rather than duplicating its form-assembly logic (ADR 0001: reuse, don't
rebuild -- avoids repeating GO-1's split-brain-data-access mistake a third
time).
"""

import httpx

from src.forms.config import forms_settings
from src.forms.exceptions import FormNotFound
from src.forms.schemas import FormDetail
from src.exceptions import UpstreamServiceError


async def get_form(form_id: str) -> FormDetail:
    async with httpx.AsyncClient(base_url=forms_settings.KOTLIN_BACKEND_URL) as client:
        response = await client.get(f"/forms/{form_id}")

    if response.status_code == 404:
        raise FormNotFound(f"Form {form_id} not found")
    if response.status_code >= 400:
        raise UpstreamServiceError(f"Kotlin backend returned {response.status_code}")

    return FormDetail.model_validate(response.json())
