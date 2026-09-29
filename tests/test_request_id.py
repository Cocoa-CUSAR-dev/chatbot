"""X-2e: what the middleware accepts, and what it puts on the way out."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app
from src.request_id import _VALID_REQUEST_ID, REQUEST_ID_HEADER


@pytest.mark.parametrize(
    ("case", "inbound"),
    [
        ("embedded newline", "abc\nlevel=ERROR forged log line"),
        ("embedded return", "abc\r"),
        ("too long", "a" * 65),
        ("space", "has a space"),
        ("empty", ""),
    ],
)
async def test_an_id_that_is_not_safe_to_log_or_forward_is_replaced(
    client: AsyncClient, case: str, inbound: str
) -> None:
    # The value is echoed back, logged on every line of the request, and
    # forwarded verbatim to web-backend and mobile-backend. Replaced, not
    # rejected: the caller still gets served, it just does not get to
    # choose what goes into our logs or theirs.
    response = await client.get("/health", headers={REQUEST_ID_HEADER: inbound})

    issued = response.headers[REQUEST_ID_HEADER]
    assert issued != inbound, f"{case} should not have been trusted"
    assert _VALID_REQUEST_ID.fullmatch(issued), f"{case} should be replaced with a safe id"


async def test_a_generated_uuid_survives_the_round_trip() -> None:
    # The other two services forward exactly this shape. If it did not
    # pass, every hop would renumber and correlation would break at the
    # first service boundary.
    inbound = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/health", headers={REQUEST_ID_HEADER: inbound})

    assert response.headers[REQUEST_ID_HEADER] == inbound


async def test_a_500_still_carries_the_request_id() -> None:
    """The response you most need to trace used to be the only one without an ID.

    ServerErrorMiddleware sits outside this middleware, so an unhandled
    exception produced a response request_id_middleware never touched.
    """

    @app.get("/_test/raises")
    async def _raises() -> None:
        raise RuntimeError("boom")

    # raise_server_exceptions is not in play here: the middleware handles
    # the exception itself, so the client sees a real 500 response.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/_test/raises", headers={REQUEST_ID_HEADER: "inbound-id-123"})
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != "/_test/raises"
        ]

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "inbound-id-123"
