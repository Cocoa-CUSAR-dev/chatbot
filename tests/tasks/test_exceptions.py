from fastapi import status

from src.exceptions import ServiceException, UpstreamServiceError
from src.tasks.exceptions import HandlerNotSupported


def test_handler_not_supported_defaults() -> None:
    exc = HandlerNotSupported()
    assert exc.status_code == status.HTTP_501_NOT_IMPLEMENTED
    assert isinstance(exc, ServiceException)
    # Deliberately NOT an UpstreamServiceError -- see the class's own
    # docstring: Go is working as intended here, not broken/unreachable.
    assert not isinstance(exc, UpstreamServiceError)
