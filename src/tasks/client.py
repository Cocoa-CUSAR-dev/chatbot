"""Task Submission Client -- write side. Calls Go's POST /service/tasks,
authenticated with a shared service key rather than a farmer's JWT cookie
(the chatbot has no such cookie for anyone -- see mobile-backend's
internal/middleware/service_auth_middleware.go for the trust model).

Go's dissection logic is real for the 4 handlers this sprint's forms use
(farm_activity, processing_record, farm_pest_disease_record, harvest) --
merged in mobile-backend's feat/dissection-standalone-handlers. A successful
call here does create a real domain row, not just a form.response one.
"""

import httpx

from src.exceptions import UpstreamServiceError
from src.tasks.config import tasks_settings
from src.tasks.exceptions import HandlerNotSupported
from src.tasks.schemas import TaskSubmission


async def submit_task(submission: TaskSubmission) -> None:
    async with httpx.AsyncClient(base_url=tasks_settings.GO_BACKEND_URL) as client:
        response = await client.post(
            "/service/tasks",
            json=submission.model_dump(),
            headers={"X-Service-Key": tasks_settings.GO_SERVICE_KEY},
        )

    if response.status_code < 400:
        return

    # Go's real response shapes (form_handler.go) -- distinguish them so
    # logs say what actually went wrong instead of one generic message.
    detail = _error_detail(response)
    if response.status_code == 401:
        raise UpstreamServiceError(
            f"Go rejected the service key (401) -- confirm GO_SERVICE_KEY here matches "
            f"CHATBOT_SERVICE_KEY on mobile-backend: {detail}"
        )
    if response.status_code == 403:
        raise UpstreamServiceError(
            f"Go found no chat.conversation for this user_id+task_id (403) -- the "
            f"submission's user_id/task_id don't match a real conversation: {detail}"
        )
    if response.status_code == 501:
        # Not an UpstreamServiceError -- Go isn't broken, it's correctly
        # saying "not built yet." confirm_conversation catches this
        # specifically to tell the farmer that honestly instead of
        # suggesting a retry that can never succeed.
        raise HandlerNotSupported(
            f"Go doesn't support automatic storage for this handler yet (501): {detail}"
        )
    raise UpstreamServiceError(f"Go backend returned {response.status_code}: {detail}")


def _error_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", response.text))
    except ValueError:
        return response.text
