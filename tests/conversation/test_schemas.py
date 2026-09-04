import uuid

import pytest
from pydantic import ValidationError

from src.conversation.schemas import (
    CancelRequest,
    ChoiceResponse,
    ConfirmRequest,
    ConversationReplyResponse,
    FormSummary,
    MessageRequest,
    StartConversationRequest,
)


def test_form_summary_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        FormSummary(task_id=uuid.uuid4(), task_form_id=uuid.uuid4())  # missing title/handler


def test_start_conversation_request_has_no_user_id_field() -> None:
    # No user_id -- router.py resolves the test farmer server-side, not from
    # the request body (see router.py's own docstring for this schema).
    request = StartConversationRequest(task_id=uuid.uuid4(), task_form_id=uuid.uuid4())
    assert not hasattr(request, "user_id")


def test_message_request_round_trip() -> None:
    conversation_id = uuid.uuid4()
    task_form_id = uuid.uuid4()
    request = MessageRequest(conversation_id=conversation_id, task_form_id=task_form_id, text="5")
    assert request.conversation_id == conversation_id
    assert request.text == "5"


def test_confirm_and_cancel_requests() -> None:
    conversation_id = uuid.uuid4()
    assert ConfirmRequest(conversation_id=conversation_id, task_form_id=uuid.uuid4())
    assert CancelRequest(conversation_id=conversation_id).conversation_id == conversation_id


def test_choice_response_round_trip() -> None:
    choice = ChoiceResponse(id="true", label="ใช่")
    assert choice.id == "true"
    assert choice.label == "ใช่"


def test_conversation_reply_response_defaults() -> None:
    reply = ConversationReplyResponse(
        conversation_id=uuid.uuid4(), substate="guided_asking_fixed_question", text="คำถาม"
    )
    assert reply.choices is None
    assert reply.input_type is None
    assert reply.validation_rule is None
