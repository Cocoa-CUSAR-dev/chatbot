from fastapi import status


class ServiceException(Exception):
    """Base class for exceptions this service raises on purpose (not bugs)."""

    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Service error"

    def __init__(self, detail: str | None = None) -> None:
        if detail:
            self.detail = detail
        super().__init__(self.detail)


class UpstreamServiceError(ServiceException):
    """Raised when Kotlin (forms) or Go (tasks) returns an unexpected response.

    Deliberately distinct from a 4xx caused by the farmer's own input -- this
    means one of the two existing backends is unreachable or broken, not that
    the request itself was bad.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    detail = "Upstream service error"
