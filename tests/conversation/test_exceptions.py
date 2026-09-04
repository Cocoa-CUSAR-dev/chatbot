from fastapi import status

from src.conversation.exceptions import AnswerInFlight, ConversationNotFound
from src.exceptions import ServiceException


def test_conversation_not_found_defaults() -> None:
    exc = ConversationNotFound()
    assert exc.status_code == status.HTTP_404_NOT_FOUND
    assert exc.detail == "Conversation not found"
    assert isinstance(exc, ServiceException)


def test_conversation_not_found_accepts_override_detail() -> None:
    # handle_answer raises this with a specific reason (e.g. "no open
    # question to answer") -- the override must actually take effect.
    exc = ConversationNotFound("Conversation has no open question to answer")
    assert exc.detail == "Conversation has no open question to answer"


def test_answer_in_flight_defaults() -> None:
    exc = AnswerInFlight()
    assert exc.status_code == status.HTTP_409_CONFLICT
    assert "already being processed" in exc.detail
