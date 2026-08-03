from fastapi import status

from src.exceptions import ServiceException, UpstreamServiceError

__all__ = ["FormNotFound", "UpstreamServiceError"]


class FormNotFound(ServiceException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Form not found"
