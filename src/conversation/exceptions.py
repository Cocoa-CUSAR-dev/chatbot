from fastapi import status

from src.exceptions import ServiceException


class ConversationNotFound(ServiceException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Conversation not found"


class AnswerInFlight(ServiceException):
    """Another message for this conversation is still being processed --
    see handle_answer's own docstring for why this is dropped rather than
    queued (first message wins).
    """

    status_code = status.HTTP_409_CONFLICT
    detail = "Another answer for this conversation is already being processed -- try again"
