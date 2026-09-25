"""Structured JSON logging (X-2c). Same field convention as the project's
other services: timestamp, level, service, request_id, message.

request_id isn't populated yet -- that's X-2e -- but anything passed via
logging's `extra={...}` is promoted to a top-level JSON field automatically,
so no changes will be needed here when that lands.
"""

import json
import logging
from datetime import UTC, datetime

SERVICE_NAME = "chatbot"

# Every attribute a stock LogRecord already carries, plus the two logging
# computes lazily (`message`, `asctime`) -- used to find the extra fields a
# caller passed via `extra={...}`.
_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    # uvicorn's internal pretty-console variant of `message` -- not
    # something worth promoting to a JSON field.
    "color_message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install the JSON formatter on the root logger and on uvicorn's own
    loggers -- uvicorn attaches its own plain-text handlers directly to
    `uvicorn`/`uvicorn.error`/`uvicorn.access` with propagation disabled, so
    configuring the root logger alone would leave HTTP access logs as the
    one non-JSON line on stdout.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False
