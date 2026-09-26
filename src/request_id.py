"""Request ID propagation (X-2e) across service boundaries: LINE webhook /
notifications endpoints -> chatbot -> mobile-backend (Go) / web-backend
(Kotlin).
"""

import contextvars
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-Id"

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def get_request_id() -> str:
    """Current request's correlation ID, or "" outside a request (e.g. a
    scheduled reminder job that isn't triggered by an inbound HTTP call).
    """
    return _request_id.get()


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Accepts an inbound X-Request-Id (from web-backend/mobile-backend
    calling in, e.g. a webhook they trigger) or generates one, makes it
    available to the rest of this request via get_request_id(), and echoes
    it back in the response header so the caller can correlate its own
    logs with this service's.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    token = _request_id.set(request_id)
    try:
        response = await call_next(request)
    finally:
        _request_id.reset(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response
