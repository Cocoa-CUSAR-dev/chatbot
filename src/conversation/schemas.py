"""DTOs for the dev-only test router (src/conversation/router.py) -- not used
by the real LINE webhook path, which builds its own replies via src/line.
"""

from uuid import UUID

from pydantic import BaseModel


class FormSummary(BaseModel):
    """One row from the dev-only `GET /conversation/test/forms` picker."""

    task_id: UUID
    task_form_id: UUID
    title: str
    handler: str


class StartConversationRequest(BaseModel):
    """No user_id -- the router resolves the test farmer itself (see
    router.py's _test_user_id), same as it resolves the form server-side.
    """

    task_id: UUID
    task_form_id: UUID


class MessageRequest(BaseModel):
    conversation_id: UUID
    task_form_id: UUID
    text: str


class ConfirmRequest(BaseModel):
    conversation_id: UUID
    task_form_id: UUID


class ChoiceResponse(BaseModel):
    id: str
    label: str


class ConversationReplyResponse(BaseModel):
    conversation_id: UUID
    substate: str
    text: str
    choices: list[ChoiceResponse] | None = None
