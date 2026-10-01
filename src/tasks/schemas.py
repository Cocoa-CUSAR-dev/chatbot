from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class TaskListItem(BaseModel):
    """One row of Go's GET /service/tasks response (queryTasksForUser,
    mobile-backend#176) -- the service-key twin of GetTasks used by the
    chatbot's LIFF to-do list (docs-and-plan#176). `status` is one of
    NOT_STARTED / IN_PROGRESS / OVERDUE / COMPLETED, computed by Go from
    close_at and whether a form.response row exists -- see that endpoint's
    own docstring for the exact ladder (multi-submit forms never reach
    COMPLETED there, by design).
    """

    task_id: UUID
    task_form_id: UUID
    title: str
    description: str | None = None
    open_at: datetime | None = None
    close_at: datetime | None = None
    handler: str
    is_multiple_submit: bool = False
    status: str


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
