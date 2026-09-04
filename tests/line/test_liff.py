from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.exceptions import UpstreamServiceError
from src.line.liff import InvalidLiffToken, verify_id_token


def _mock_response(status_code: int, body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return response


def _patched_client(response: MagicMock):
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("src.line.liff.httpx.AsyncClient", return_value=client)


async def test_verify_id_token_returns_claims_on_success() -> None:
    claims = {"sub": "Uabc123", "exp": 1234567890}
    with _patched_client(_mock_response(200, claims)):
        result = await verify_id_token("token-abc", "channel-1")

    assert result == claims


async def test_verify_id_token_raises_invalid_on_400() -> None:
    with (
        _patched_client(_mock_response(400, {"error": "invalid_request"})),
        pytest.raises(InvalidLiffToken),
    ):
        await verify_id_token("bad-token", "channel-1")


async def test_verify_id_token_raises_upstream_error_on_other_failures() -> None:
    with (
        _patched_client(_mock_response(500, {})),
        pytest.raises(UpstreamServiceError),
    ):
        await verify_id_token("token-abc", "channel-1")
