from uuid import UUID

from pydantic import BaseModel, Field


class SendNotificationRequest(BaseModel):
    user_ids: list[UUID] = Field(min_length=1)
    # LINE's own hard limit on a single text message is 5000 characters.
    message: str = Field(min_length=1, max_length=5000)


class FailedRecipient(BaseModel):
    user_id: UUID
    # "no_line_identity"  -- the user has no linked LINE account to push to
    # "line_api_error"    -- LINE rejected or the delivery call raised
    reason: str


class SendNotificationResponse(BaseModel):
    """Partial success: `sent` and `failed` together account for every
    distinct user_id in the request. A request never fails as a whole just
    because one recipient is unreachable.
    """

    sent: list[UUID]
    failed: list[FailedRecipient]
