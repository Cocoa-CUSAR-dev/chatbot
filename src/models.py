"""Shared model helpers used across domain modules."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


def uuid_pk() -> Mapped[uuid.UUID]:
    """Matches this project's existing convention: uuid PK via gen_random_uuid(),
    named `<entity>_id` (never a generic `id`) -- e.g. `conversation_id`,
    `line_identity_id`. Assign the return value to the correctly-named
    attribute in each model rather than using a mixin, since the column name
    itself varies per table.
    """
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
