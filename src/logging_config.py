"""Structured JSON logging (X-2c) with request-ID correlation (X-2e).

Same field convention as the project's other services: timestamp, level,
service, request_id, message.

Propagating X-Request-Id between services (src/request_id.py) only pays
off if the ID actually reaches a log line. Python's default configuration
knows nothing about our contextvar, so without RequestIdFilter below the
header travels correctly all the way to web-backend and mobile-backend and
there is still nothing to correlate in the logs. The same gap is closed on
the other two services by web-backend's ``logging.pattern.level`` and
mobile-backend's ``gin.LoggerWithFormatter``.
"""

import json
import logging
import logging.config
import sys
from datetime import UTC, datetime

from src.request_id import get_request_id

SERVICE_NAME = "chatbot"

#: Rendered for records logged outside a request -- startup, shutdown, and
#: the APScheduler reminder jobs, which are not triggered by an inbound
#: HTTP call and so legitimately have no correlation ID.
NO_REQUEST_ID = "-"

# Every attribute a stock LogRecord already carries, plus the two logging
# computes lazily (`message`, `asctime`) -- used to find the extra fields a
# caller (or a filter, like RequestIdFilter below) attached.
_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    # uvicorn's internal pretty-console variant of `message` -- not
    # something worth promoting to a JSON field.
    "color_message",
}


class RequestIdFilter(logging.Filter):
    """Copies the current request's ID onto every record passing through.

    A filter rather than passing request_id via extra={...} at each call
    site, because it has to apply to records from libraries too (httpx,
    apscheduler, uvicorn), which build their own records and know nothing
    about ``request_id``. Attached to the handler, so one instance covers
    every logger. JsonFormatter below promotes the resulting
    ``record.request_id`` to a top-level JSON field exactly the way it
    would an explicit extra={...} value -- it has no way to tell the two
    apart, which is the point: nothing here needs to special-case it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or NO_REQUEST_ID
        return True  # never actually filters anything out


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
            if key in _RESERVED_ATTRS:
                continue
            # litellm (src/llm/client.py) stamps a bare `object()` sentinel
            # (`record.litellm_redacted`) on LogRecords it has already
            # redacted secrets from -- an internal `is`-identity marker for
            # its own use, never meant to be read. `type(value) is object`
            # catches that (and any other library doing the same sentinel
            # trick) generically, rather than blocklisting attribute names
            # one at a time: a bare `object()` instance carries no data at
            # all, so there's nothing to promote either way -- its
            # json.dumps(default=str) fallback is just its memory address.
            if type(value) is object:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON, request-ID-aware handler as the only log handler.

    Called at import time in src/main.py. uvicorn configures its own
    logging before it imports the app, so running afterwards is what lets
    this replace uvicorn's handlers rather than being replaced by them.

    One known gap: uvicorn's ``access`` log is emitted after the response
    has left the middleware stack, by which point the contextvar has been
    reset, so those lines carry ``request_id: "-"``. Application logs --
    the ones that say what actually happened -- carry the real ID.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            # Loggers are created at import time all over src/ (module-level
            # getLogger calls); disabling them here would silence them.
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": "json",
                    "filters": ["request_id"],
                }
            },
            "root": {"handlers": ["default"], "level": level},
            "loggers": {
                # uvicorn attaches its own handlers and does not propagate;
                # left alone, its lines would keep the old format and the
                # output would be half formatted one way and half another.
                name: {"handlers": ["default"], "level": level, "propagate": False}
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
            },
        }
    )
