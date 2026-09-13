from fastapi import status

from src.exceptions import ServiceException
from src.line.exceptions import InvalidLineSignature


def test_invalid_line_signature_defaults() -> None:
    exc = InvalidLineSignature()
    assert exc.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.detail == "Invalid LINE webhook signature"
    assert isinstance(exc, ServiceException)
