from fastapi import status

from src.exceptions import ServiceException


class InvalidLineSignature(ServiceException):
    status_code = status.HTTP_400_BAD_REQUEST
    detail = "Invalid LINE webhook signature"
