from fastapi import status

from src.exceptions import ServiceException


class InvalidLinkCode(ServiceException):
    """Code doesn't exist, already used, or expired -- ADR 0002."""

    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Invalid or expired link code"
