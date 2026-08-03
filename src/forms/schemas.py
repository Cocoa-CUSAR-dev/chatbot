from typing import Any

from pydantic import BaseModel


class FormDetail(BaseModel):
    """Loosely typed on purpose -- this mirrors whatever Kotlin's existing
    GET /forms/{formId} already returns (ADR 0001: reuse, don't rebuild).
    Tighten this once the real response shape is pinned down against Kotlin.
    """

    model_config = {"extra": "allow"}

    task_form_id: str
    sections: list[dict[str, Any]] = []
