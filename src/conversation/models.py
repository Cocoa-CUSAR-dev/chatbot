"""ORM models for chat.conversation / chat.conversation_answer -- schema owned
by the `database` repo's Flyway migrations (ADR 0005), not created here.
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base
from src.models import uuid_pk


class Conversation(Base):
    __tablename__ = "conversation"
    __table_args__ = {"schema": "chat"}

    conversation_id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth.user_account.user_id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form.task.task_id"))
    task_form_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String)
    current_question_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class ConversationAnswer(Base):
    __tablename__ = "conversation_answer"
    __table_args__ = {"schema": "chat"}

    conversation_answer_id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat.conversation.conversation_id")
    )
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("form.question.question_id"))
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String)
