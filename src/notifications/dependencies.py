import secrets
from typing import Annotated

from fastapi import Header

from src.notifications.config import notifications_settings
from src.notifications.exceptions import InvalidServiceKey


async def require_service_key(
    x_service_key: Annotated[str | None, Header()] = None,
) -> None:
    """FastAPI dependency: reject the request unless it carries a valid
    X-Service-Key header. Same trust model as mobile-backend's
    service_auth_middleware.go / web-backend's ServiceKeyFilter -- a shared
    secret between first-party services, not a per-user credential.
    """
    expected = notifications_settings.CHATBOT_SERVICE_KEY
    if not x_service_key or not secrets.compare_digest(x_service_key, expected):
        raise InvalidServiceKey()
