"""src/sso/client.py: minting the token behind the diary card's "view
history" link (docs-and-plan#247). The router falls back to a plain /history
link on any failure here, so every failure must raise.
"""

import json

import pytest
import respx
from httpx import Response

from src.exceptions import UpstreamServiceError
from src.sso.client import mint_sso_token
from src.sso.config import sso_settings

_URL = f"{sso_settings.KOTLIN_BACKEND_URL}/service/sso/tokens"


@respx.mock
async def test_returns_the_token_and_sends_the_service_key() -> None:
    route = respx.post(_URL).mock(return_value=Response(200, json={"value": "tok", "error": None}))

    assert await mint_sso_token("u-1") == "tok"
    request = route.calls.last.request
    assert json.loads(request.content) == {"userId": "u-1"}
    assert request.headers["X-Service-Key"] == sso_settings.KOTLIN_SERVICE_KEY


@respx.mock
async def test_401_names_the_service_key_mismatch() -> None:
    respx.post(_URL).mock(return_value=Response(401))
    with pytest.raises(UpstreamServiceError, match="KOTLIN_SERVICE_KEY"):
        await mint_sso_token("u-1")


@respx.mock
async def test_other_errors_raise() -> None:
    respx.post(_URL).mock(return_value=Response(500))
    with pytest.raises(UpstreamServiceError, match="500"):
        await mint_sso_token("u-1")


@respx.mock
async def test_a_200_carrying_an_error_envelope_raises() -> None:
    respx.post(_URL).mock(return_value=Response(200, json={"value": None, "error": "no user"}))
    with pytest.raises(UpstreamServiceError, match="no user"):
        await mint_sso_token("u-1")
