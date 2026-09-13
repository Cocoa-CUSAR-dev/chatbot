from fastapi import status

from src.exceptions import ServiceException


class InvalidServiceKey(ServiceException):
    """Raised when a request to a /service/* endpoint is missing the
    X-Service-Key header or its value doesn't match CHATBOT_SERVICE_KEY.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    detail = "Invalid or missing service key"
