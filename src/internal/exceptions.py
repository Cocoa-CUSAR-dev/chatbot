from fastapi import status

from src.exceptions import ServiceException


class InvalidCronSecret(ServiceException):
    """Raised when a request to /internal/cron/* is missing the Authorization
    header or its value doesn't match CRON_SECRET.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    detail = "Invalid or missing cron secret"
