from typing import Any

from pydantic import BaseModel


class TaskSubmission(BaseModel):
    """Same shape Go's POST /tasks already expects (ADR 0001). This is also
    reused as the LLM's structured-output schema for extraction (ADR 0004) --
    one schema definition, two consumers, per ADR 0003's Pydantic choice.
    """

    task_id: str
    answer: dict[str, Any]
