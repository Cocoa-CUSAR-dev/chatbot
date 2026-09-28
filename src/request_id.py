"""Request ID propagation (X-2e) across service boundaries: LINE webhook /
notifications endpoints -> chatbot -> mobile-backend (Go) / web-backend
(Kotlin).
"""

import contextvars
import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"

#: What an inbound ID has to look like to be trusted. The value is echoed
#: back, logged on every line of the request, and forwarded verbatim to
#: web-backend and mobile-backend, so an oversized or newline-bearing one
#: would follow us into both. Anything else is replaced with a fresh ID
#: rather than rejected -- the caller still gets served. web-backend's
#: RequestIdFilter and mobile-backend's requestid.Middleware enforce the
#: same shape, so an ID that survives one hop survives the next.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

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

    Unhandled exceptions are turned into a 500 here rather than being left
    to Starlette's ServerErrorMiddleware, which sits *outside* this
    middleware: an exception reaching it would produce a response this
    function never touches, so the 500 -- the one response anybody
    actually needs to trace -- was the only one arriving with no ID on it.
    Handling it here also means it gets logged with the ID attached (see
    src/logging_config.py), next to whatever else the request logged.
    """
    inbound = request.headers.get(REQUEST_ID_HEADER, "")
    # fullmatch, not match: "abc<newline>evil" starts with something valid.
    request_id = inbound if _VALID_REQUEST_ID.fullmatch(inbound) else str(uuid.uuid4())

    token = _request_id.set(request_id)
    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled error in %s %s", request.method, request.url.path)
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error"},
            )
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        _request_id.reset(token)
