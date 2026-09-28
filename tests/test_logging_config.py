import json
import logging
import sys
import uuid

from src.logging_config import JsonFormatter


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
