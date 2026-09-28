from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.exceptions import UpstreamServiceError
from src.tasks.client import submit_task
from src.tasks.exceptions import HandlerNotSupported
from src.tasks.schemas import TaskSubmission


def _mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body or {}
    response.text = ""
    return response


def _patched_client(response: MagicMock):
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.tasks.client.httpx.AsyncClient", return_value=client)


_SUBMISSION = TaskSubmission(user_id="u-1", task_id="t-1", answer={"farm_id": "f-1"})


async def test_submit_task_returns_on_success() -> None:
    with _patched_client(_mock_response(200)):
        await submit_task(_SUBMISSION)  # no exception -- success


async def test_submit_task_forwards_request_id() -> None:
    """X-2e: a single farmer action must be traceable across chatbot's and
    mobile-backend's logs -- confirms the header is actually sent.
    """
    with (
        _patched_client(_mock_response(200)) as mock_client_cls,
        patch("src.tasks.client.get_request_id", return_value="test-request-id-xyz"),
    ):
        await submit_task(_SUBMISSION)

    client = mock_client_cls.return_value
    _, kwargs = client.post.call_args
    assert kwargs["headers"]["X-Request-Id"] == "test-request-id-xyz"


async def test_submit_task_401_raises_upstream_service_error() -> None:
    with (
        _patched_client(_mock_response(401, {"error": "bad key"})),
        pytest.raises(UpstreamServiceError, match="rejected the service key"),
    ):
        await submit_task(_SUBMISSION)


async def test_submit_task_403_raises_upstream_service_error() -> None:
    with (
        _patched_client(_mock_response(403, {"error": "no matching conversation"})),
        pytest.raises(UpstreamServiceError, match="no chat.conversation"),
    ):
        await submit_task(_SUBMISSION)


async def test_submit_task_501_raises_handler_not_supported() -> None:
    with (
        _patched_client(_mock_response(501, {"error": "not built yet"})),
        pytest.raises(HandlerNotSupported),
    ):
        await submit_task(_SUBMISSION)


async def test_submit_task_other_error_raises_generic_upstream_error() -> None:
    with (
        _patched_client(_mock_response(500, {"error": "boom"})),
        pytest.raises(UpstreamServiceError, match="500"),
    ):
        await submit_task(_SUBMISSION)
