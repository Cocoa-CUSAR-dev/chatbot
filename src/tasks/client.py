"""Task Submission Client -- write side. Calls Go's EXISTING POST /tasks.

Depends on GO-2 (SubmitTask's real dissection logic) actually being finished --
see the backend-fix-proposal doc and ADR 0001. Until that lands, submissions
through this client will silently not create real domain rows on Go's side,
even though this client itself works correctly.
"""

import httpx

from src.exceptions import UpstreamServiceError
from src.tasks.config import tasks_settings
from src.tasks.schemas import TaskSubmission


async def submit_task(submission: TaskSubmission) -> None:
    async with httpx.AsyncClient(base_url=tasks_settings.GO_BACKEND_URL) as client:
        response = await client.post("/tasks", json=submission.model_dump())

    if response.status_code >= 400:
        raise UpstreamServiceError(f"Go backend returned {response.status_code}")
