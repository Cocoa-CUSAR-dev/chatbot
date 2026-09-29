import json
import logging
import sys
import uuid

from httpx import AsyncClient

from src.logging_config import NO_REQUEST_ID, JsonFormatter, RequestIdFilter
from src.main import app
from src.request_id import _request_id


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="chatbot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_the_standard_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record("something happened")))
    assert payload["level"] == "INFO"
    assert payload["service"] == "chatbot"
    assert payload["logger"] == "chatbot.test"
    assert payload["message"] == "something happened"
    assert "timestamp" in payload


def test_promotes_a_caller_supplied_extra_field() -> None:
    # The documented contract: anything passed via extra={...} becomes a
    # top-level JSON field (see the module's own docstring and X-2e).
    payload = json.loads(JsonFormatter().format(_record(user_id="u123")))
    assert payload["user_id"] == "u123"


def test_drops_a_bare_object_sentinel_instead_of_leaking_its_memory_address() -> None:
    # litellm (src/llm/client.py) stamps record.litellm_redacted with a bare
    # object() sentinel it uses for its own is-identity bookkeeping -- see
    # litellm/_logging.py's _REDACTED_STAMP. Promoting it verbatim used to
    # serialize as something like "<object object at 0x...>" in every log
    # line litellm touches, on every server boot, carrying zero information.
    payload = json.loads(JsonFormatter().format(_record(litellm_redacted=object())))
    assert "litellm_redacted" not in payload


def test_a_real_value_needing_str_conversion_still_comes_through() -> None:
    # The object-sentinel skip must not become a blanket "skip anything not
    # natively JSON-serializable" -- json.dumps(default=str)'s existing
    # fallback is still how a non-trivial-but-real value (e.g. a UUID,
    # Decimal, datetime) reaches the log line.
    request_id = uuid.uuid4()
    payload = json.loads(JsonFormatter().format(_record(request_id=request_id)))
    assert payload["request_id"] == str(request_id)


def test_formats_exception_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]


# --- request-ID correlation (X-2e) -----------------------------------------


def test_filter_puts_the_current_request_id_on_the_record() -> None:
    token = _request_id.set("test-request-id-abc")
    try:
        record = _record()
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "test-request-id-abc"  # type: ignore[attr-defined]
    finally:
        _request_id.reset(token)


def test_filter_falls_back_outside_a_request() -> None:
    # Reminder jobs are fired by APScheduler, not by an inbound HTTP call,
    # so they have no correlation ID and must still log.
    record = _record()
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == NO_REQUEST_ID  # type: ignore[attr-defined]


def test_filter_and_formatter_agree_on_the_field_name() -> None:
    """The filter sets record.request_id; JsonFormatter promotes whatever
    extra attribute it finds. Testing them separately would pass even if
    one side were renamed, which is exactly the silent failure this whole
    change is guarding against.
    """
    token = _request_id.set("test-request-id-abc")
    try:
        record = _record("diary generation failed")
        RequestIdFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        _request_id.reset(token)

    assert payload["request_id"] == "test-request-id-abc"
    assert payload["message"] == "diary generation failed"


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
    handler.setFormatter(JsonFormatter())
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
    payload = json.loads(captured[0])
    assert payload["request_id"] == "inbound-id-123"
    assert payload["message"] == "submitting task"
