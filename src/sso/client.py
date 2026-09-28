"""SSO Client -- mints a short-lived token for the diary card's "view full
history" deep link (US2-6, docs-and-plan#133). Same service-key trust model
as src/diary/client.py's generate_diary: the chatbot already knows this is
the right user_id (it just used it to generate the diary), so minting a
token for them isn't a new trust decision, just handing the browser proof
of one already made.
"""

import httpx

from src.exceptions import UpstreamServiceError
from src.request_id import REQUEST_ID_HEADER, get_request_id
from src.sso.config import sso_settings


async def mint_sso_token(user_id: str) -> str:
    async with httpx.AsyncClient(base_url=sso_settings.KOTLIN_BACKEND_URL, timeout=30.0) as client:
        response = await client.post(
            "/service/sso/tokens",
            json={"userId": user_id},
            headers={
                "X-Service-Key": sso_settings.KOTLIN_SERVICE_KEY,
                REQUEST_ID_HEADER: get_request_id(),
            },
        )

    if response.status_code == 401:
        raise UpstreamServiceError(
            "Kotlin rejected the service key (401) -- confirm KOTLIN_SERVICE_KEY here "
            "matches CHATBOT_SERVICE_KEY on web-backend"
        )
    if response.status_code >= 400:
        raise UpstreamServiceError(f"Kotlin backend returned {response.status_code}")

    body = response.json()
    error = body.get("error")
    if error:
        raise UpstreamServiceError(f"Kotlin returned an error envelope: {error}")

    token: str = body["value"]
    return token
