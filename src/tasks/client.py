"""Task Submission Client -- write side. Calls Go's POST /service/tasks,
authenticated with a shared service key rather than a farmer's JWT cookie
(the chatbot has no such cookie for anyone -- see mobile-backend's
internal/middleware/service_auth_middleware.go for the trust model).

Go's dissection logic is real for the 4 handlers this sprint's forms use
(farm_activity, processing_record, farm_pest_disease_record, harvest) --
merged in mobile-backend's feat/dissection-standalone-handlers. A successful
call here does create a real domain row, not just a form.response one.
"""

from typing import Any

import httpx

from src.exceptions import UpstreamServiceError
from src.tasks.config import tasks_settings
from src.tasks.exceptions import HandlerNotSupported
from src.tasks.schemas import TaskSubmission


async def submit_task(submission: TaskSubmission) -> None:
    # 30s, not httpx's 5s default -- Go's own liveColumns cache (form_handler
    # .go) has a one-time cold-cache cost per destination table per process
    # lifetime, same shape as Kotlin's fetchRefChoices (BE-5). Live-caught
    # 2026-08-19: the very first submission ever made against
    # agriculture.farm_activity_fertilizer (one of the 5 newly-unblocked
    # handlers) took 6.6s end to end on Go's side and actually succeeded --
    # but this client's old 5s default timed out first, so confirm_conversation
    # told the farmer it failed and to retry, when retrying would have
    # inserted a second, duplicate row (dissectAnswer has no idempotency
    # guard). Same fix, same reasoning as src/forms/client.py's get_form().
    async with httpx.AsyncClient(base_url=tasks_settings.GO_BACKEND_URL, timeout=30.0) as client:
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


async def fetch_last_answer(*, user_id: str, handler: str) -> dict[str, Any] | None:
    """GET Go's /service/tasks/last-answer (#100, US2-4) -- the raw answer
    JSON from this farmer's most recent COMPLETED submission for `handler`,
    or None if there isn't one yet. Deliberately unfiltered: task_id, stale
    parent IDs (farm_activity_id/harvest_id/batch_id), and OPTION values
    that may no longer resolve in the CURRENT form are all still in here --
    src.conversation.reuse.sanitize_for_autofill is what strips those
    before anything gets offered to a farmer, not this function.
    """
    async with httpx.AsyncClient(base_url=tasks_settings.GO_BACKEND_URL, timeout=30.0) as client:
        response = await client.get(
            "/service/tasks/last-answer",
            params={"user_id": user_id, "handler": handler},
            headers={"X-Service-Key": tasks_settings.GO_SERVICE_KEY},
        )

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise UpstreamServiceError(
            f"Go backend returned {response.status_code} for last-answer lookup: "
            f"{_error_detail(response)}"
        )

    body = response.json()
    answer = body.get("answer")
    if not isinstance(answer, dict):
        raise UpstreamServiceError(
            f"Go's last-answer response had no usable 'answer' object: {body!r}"
        )
    return answer


async def fetch_sanitized_autofill(
    *, answer: dict[str, Any], questions: list[dict[str, Any]]
) -> dict[str, Any]:
    """POST Go's /service/autofill/sanitize (#105, US2-5) -- the ONE shared
    filtering implementation both chat and any remaining static form call,
    so the two channels can't drift apart on what's safe to prefill a
    farmer with from a past submission. `questions` must already be in
    mobile-backend's internal/validation.Question JSON shape
    (fieldName/inputType/choices, choices as {id, name}) --
    src.conversation.reuse.sanitize_for_autofill builds that from this
    chatbot's own Question dataclasses; this function is just the HTTP call.
    """
    async with httpx.AsyncClient(base_url=tasks_settings.GO_BACKEND_URL, timeout=30.0) as client:
        response = await client.post(
            "/service/autofill/sanitize",
            json={"answer": answer, "questions": questions},
            headers={"X-Service-Key": tasks_settings.GO_SERVICE_KEY},
        )

    if response.status_code >= 400:
        raise UpstreamServiceError(
            f"Go backend returned {response.status_code} for autofill sanitize: "
            f"{_error_detail(response)}"
        )

    body = response.json()
    sanitized = body.get("answer")
    if not isinstance(sanitized, dict):
        raise UpstreamServiceError(
            f"Go's autofill-sanitize response had no usable 'answer' object: {body!r}"
        )
    return sanitized


def _error_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", response.text))
    except ValueError:
        return response.text
