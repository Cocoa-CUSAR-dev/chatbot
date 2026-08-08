from typing import Any

from pydantic import BaseModel


class TaskSubmission(BaseModel):
    """Matches Go's POST /service/tasks (ADR 0001's write side, via the
    chatbot's own service-account credential -- see tasks/client.py).
    user_id is explicit here because there's no farmer session for Go to
    derive it from on this path; Go independently verifies a matching
    chat.conversation row exists before trusting it.

    Also reused as the LLM's structured-output schema for extraction
    (ADR 0004) -- one schema definition, two consumers, per ADR 0003's
    Pydantic choice.
    """

    user_id: str
    task_id: str
    answer: dict[str, Any]
