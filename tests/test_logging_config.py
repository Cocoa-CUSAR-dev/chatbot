"""X-2e: the header only becomes useful once it reaches a log line."""

import logging

from httpx import AsyncClient

from src.logging_config import LOG_FORMAT, NO_REQUEST_ID, RequestIdFilter
from src.main import app
from src.request_id import _request_id


def _record() -> logging.LogRecord:
    return logging.LogRecord(
        name="src.line.router",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="diary generation failed",
        args=(),
        exc_info=None,
    )


def test_filter_puts_the_current_request_id_on_the_record() -> None:
    token = _request_id.set("test-request-id-abc")
    try:
        record = _record()
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "test-request-id-abc"
    finally:
        _request_id.reset(token)


def test_filter_falls_back_outside_a_request() -> None:
    # Reminder jobs are fired by APScheduler, not by an inbound HTTP call,
    # so they have no correlation ID and must still log.
    record = _record()
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == NO_REQUEST_ID


def test_formatted_line_actually_contains_the_id() -> None:
    """The filter and the format string have to agree on the field name.

    Testing them separately would pass even if one of them were renamed,
    which is exactly the silent failure this whole change is fixing.
    """
    token = _request_id.set("test-request-id-abc")
    try:
        record = _record()
        RequestIdFilter().filter(record)
        line = logging.Formatter(LOG_FORMAT).format(record)
    finally:
        _request_id.reset(token)

    assert "request_id=test-request-id-abc" in line
    assert "diary generation failed" in line


async def test_application_log_during_a_request_carries_that_request_s_id(
    client: AsyncClient,
) -> None:
    """End to end: an inbound X-Request-Id reaches a log line.

    The log call happens inside a real route, driven through the real
    middleware stack, so this fails if the middleware is dropped from
    src/main.py just as surely as if the filter is misconfigured -- the
    two halves that have to agree for any of this to be worth having.
    """
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())

    logger = logging.getLogger("tests.during_request")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    @app.get("/_test/logs-a-line")
    async def _logs_a_line() -> dict[str, str]:
        logger.info("submitting task")
        return {"ok": "true"}

    try:
        response = await client.get(
            "/_test/logs-a-line", headers={"X-Request-Id": "inbound-id-123"}
        )
    finally:
        logger.removeHandler(handler)
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != "/_test/logs-a-line"
        ]

    assert response.headers["X-Request-Id"] == "inbound-id-123"
    assert len(captured) == 1
    assert "request_id=inbound-id-123" in captured[0]
    assert "submitting task" in captured[0]
