"""Log output configuration (X-2e).

Propagating X-Request-Id between services (src/request_id.py) only pays
off if the ID actually reaches a log line. Python's default configuration
knows nothing about our contextvar, so without this module the header
travels correctly all the way to web-backend and mobile-backend and there
is still nothing to correlate in the logs.

The same gap is closed on the other two services by web-backend's
``logging.pattern.level`` and mobile-backend's ``gin.LoggerWithFormatter``.
"""

import logging
import logging.config
import sys

from src.request_id import get_request_id

#: Rendered for records logged outside a request -- startup, shutdown, and
#: the APScheduler reminder jobs, which are not triggered by an inbound
#: HTTP call and so legitimately have no correlation ID.
NO_REQUEST_ID = "-"

LOG_FORMAT = "%(asctime)s %(levelname)-8s [request_id=%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    """Copies the current request's ID onto every record passing through.

    A filter rather than a custom formatter because it has to apply to
    records from libraries too (httpx, apscheduler, uvicorn), which build
    their own records and know nothing about ``request_id``. Attached to
    the handler, so one instance covers every logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or NO_REQUEST_ID
        return True  # never actually filters anything out


def configure_logging(level: str = "INFO") -> None:
    """Install the request-ID-aware handler as the only log handler.

    Called at import time in src/main.py. uvicorn configures its own
    logging before it imports the app, so running afterwards is what lets
    this replace uvicorn's handlers rather than being replaced by them.

    One known gap: uvicorn's ``access`` log is emitted after the response
    has left the middleware stack, by which point the contextvar has been
    reset, so those lines show ``request_id=-``. Application logs -- the
    ones that say what actually happened -- carry the real ID.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            # Loggers are created at import time all over src/ (module-level
            # getLogger calls); disabling them here would silence them.
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {"default": {"format": LOG_FORMAT}},
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": "default",
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
