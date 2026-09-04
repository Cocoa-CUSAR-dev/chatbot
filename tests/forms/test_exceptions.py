from fastapi import status

from src.exceptions import ServiceException
from src.forms.exceptions import FormNotFound


def test_form_not_found_defaults() -> None:
    exc = FormNotFound()
    assert exc.status_code == status.HTTP_404_NOT_FOUND
    assert exc.detail == "Form not found"
    assert isinstance(exc, ServiceException)
