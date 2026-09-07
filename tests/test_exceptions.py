from fastapi import status

from src.exceptions import ServiceException, UpstreamServiceError


def test_service_exception_uses_class_default_detail() -> None:
    exc = ServiceException()
    assert exc.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.detail == "Service error"
    assert str(exc) == "Service error"


def test_service_exception_accepts_override_detail() -> None:
    exc = ServiceException("something specific went wrong")
    assert exc.detail == "something specific went wrong"
    assert str(exc) == "something specific went wrong"


def test_upstream_service_error_defaults() -> None:
    exc = UpstreamServiceError()
    assert exc.status_code == status.HTTP_502_BAD_GATEWAY
    assert exc.detail == "Upstream service error"


def test_upstream_service_error_is_a_service_exception() -> None:
    # main.py's service_exception_handler is registered on ServiceException
    # itself -- every subclass must actually inherit it or it won't be caught.
    assert issubclass(UpstreamServiceError, ServiceException)
