import secrets
from typing import Annotated

from fastapi import Header

from src.internal.config import internal_settings
from src.internal.exceptions import InvalidCronSecret


async def require_cron_secret(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency guarding /internal/cron/* -- same shared-secret
    trust model as src.notifications' X-Service-Key, just read from the
    standard Authorization header instead: that's the one Vercel's own Cron
    Jobs send (as "Bearer <CRON_SECRET>") without any extra configuration on
    our side, and it's an equally normal header for a GitHub Actions
    schedule (or anything else external) to send the same secret in.
    """
    expected = f"Bearer {internal_settings.CRON_SECRET}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise InvalidCronSecret()
