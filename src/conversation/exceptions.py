from fastapi import status

from src.exceptions import ServiceException


class ConversationNotFound(ServiceException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Conversation not found"
