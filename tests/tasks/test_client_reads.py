"""src/tasks/client.py's read-side Go calls, through real httpx with respx
(docs-and-plan#247): the to-do list, the last answer, and the autofill
sanitizer. test_client.py covers submit_task.
"""

import json
import uuid

import pytest
import respx
from httpx import Response

from src.exceptions import UpstreamServiceError
from src.tasks.client import fetch_last_answer, fetch_pending_tasks, fetch_sanitized_autofill
from src.tasks.config import tasks_settings

_GO = tasks_settings.GO_BACKEND_URL


@respx.mock
async def test_pending_tasks_parses_every_row_and_asks_for_one_big_page() -> None:
    task_id, task_form_id = uuid.uuid4(), uuid.uuid4()
    route = respx.get(f"{_GO}/service/tasks").mock(
        return_value=Response(
            200,
            json=[
                {
                    "task_id": str(task_id),
                    "task_form_id": str(task_form_id),
                    "title": "จดกิจกรรมในสวน",
                    "handler": "farm_activity",
                    "status": "NOT_STARTED",
                }
            ],
        )
    )

    [task] = await fetch_pending_tasks(user_id="u-1")

    assert (task.task_id, task.task_form_id, task.status) == (task_id, task_form_id, "NOT_STARTED")
    request = route.calls.last.request
    assert (request.url.params["user_id"], request.url.params["page_size"]) == ("u-1", "100")
    assert request.headers["X-Service-Key"] == tasks_settings.GO_SERVICE_KEY


@respx.mock
async def test_pending_tasks_error_carries_gos_reason() -> None:
    respx.get(f"{_GO}/service/tasks").mock(return_value=Response(500, json={"error": "db down"}))
    with pytest.raises(UpstreamServiceError, match="db down"):
        await fetch_pending_tasks(user_id="u-1")


@respx.mock
async def test_last_answer_rejects_a_body_without_an_answer_object() -> None:
    respx.get(f"{_GO}/service/tasks/last-answer").mock(
        return_value=Response(200, json={"answer": "not a dict"})
    )
    with pytest.raises(UpstreamServiceError, match="no usable 'answer'"):
        await fetch_last_answer(user_id="u-1", handler="notes")


@respx.mock
async def test_last_answer_error_includes_gos_validation_details() -> None:
    """Go's 400s put the per-field reasons in "details"; without them the
    log would only say "validation failed"."""
    respx.get(f"{_GO}/service/tasks/last-answer").mock(
        return_value=Response(
            400, json={"error": "validation failed", "details": ["handler: required"]}
        )
    )
    with pytest.raises(UpstreamServiceError, match="validation failed -- handler: required"):
        await fetch_last_answer(user_id="u-1", handler="")


@respx.mock
async def test_sanitize_sends_the_raw_answer_and_returns_the_filtered_one() -> None:
    route = respx.post(f"{_GO}/service/autofill/sanitize").mock(
        return_value=Response(200, json={"answer": {"notes": "ค่าเดิม"}})
    )

    result = await fetch_sanitized_autofill(answer={"notes": "ค่าเดิม", "gis": [1]}, questions=[])

    assert result == {"notes": "ค่าเดิม"}
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"answer": {"notes": "ค่าเดิม", "gis": [1]}, "questions": []}


@respx.mock
async def test_sanitize_error_with_a_non_json_body_keeps_the_raw_text() -> None:
    respx.post(f"{_GO}/service/autofill/sanitize").mock(
        return_value=Response(502, text="<html>Bad Gateway</html>")
    )
    with pytest.raises(UpstreamServiceError, match="Bad Gateway"):
        await fetch_sanitized_autofill(answer={}, questions=[])


@respx.mock
async def test_sanitize_rejects_a_body_without_an_answer_object() -> None:
    respx.post(f"{_GO}/service/autofill/sanitize").mock(return_value=Response(200, json={}))
    with pytest.raises(UpstreamServiceError, match="no usable 'answer'"):
        await fetch_sanitized_autofill(answer={}, questions=[])
