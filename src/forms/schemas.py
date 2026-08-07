from typing import Any

from pydantic import BaseModel


class FormDetail(BaseModel):
    """Loosely typed on purpose -- this is the shape AFTER forms/client.py's
    get_form() has already unwrapped Kotlin's {value, error} envelope and
    converted its camelCase keys to snake_case. Each dict in
    sections[]["questions"] carries question_id/label/field_name/
    is_mandatory/sort_order (and, for OPTION-type questions, a "choices"
    list of {id, name} -- not yet consumed by anything, since choice-based
    questions aren't handled in the guided flow yet).
    """

    model_config = {"extra": "allow"}

    task_form_id: str
    sections: list[dict[str, Any]] = []
