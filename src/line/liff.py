"""LIFF ID-token verification.

Not part of `line-bot-sdk` -- `linebot.v3.liff` is for managing LIFF apps
(add/update/list), not for verifying a logged-in LIFF session's ID token.
That's a plain REST call to LINE's own OAuth2 verify endpoint, so this uses
httpx directly, same as src/forms and src/tasks.
"""

from typing import Any, cast

import httpx
from fastapi import status

from src.exceptions import ServiceException, UpstreamServiceError

_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"


class InvalidLiffToken(ServiceException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Invalid or expired LIFF ID token"


async def verify_id_token(id_token: str, channel_id: str) -> dict[str, Any]:
    """Returns the decoded claims (includes `sub` -- the LINE user ID) if
    valid. Raises InvalidLiffToken on anything else.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _VERIFY_URL,
            data={"id_token": id_token, "client_id": channel_id},
        )

    if response.status_code == 400:
        raise InvalidLiffToken
    if response.status_code >= 400:
        raise UpstreamServiceError(f"LINE verify endpoint returned {response.status_code}")

    # httpx's .json() is typed Any (JSON content can be anything) -- LINE's
    # verify endpoint contract guarantees an object on 200, per its own docs.
    return cast(dict[str, Any], response.json())
