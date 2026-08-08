"""ORM models for chat.conversation / chat.conversation_answer -- schema owned
by the `database` repo's Flyway migrations (ADR 0005), not created here.

The FKs below point at auth.user_account / form.task / form.question --
tables this service deliberately has no ORM models for (src/database.py:
"never touches form.*/agriculture.*/etc. domain tables directly", those are
Go/Kotlin's domain). But SQLAlchemy still needs a Table object registered in
THIS Base's metadata to resolve a ForeignKey string at flush time -- without
one, session.commit() fails with NoReferencedTableError (hit for real while
testing Sprint 1's guided-flow engine). The stub Tables below exist purely
for that FK resolution; they're bare Core Tables; not mapped classes, so
this module still has no actual ORM access to those schemas.
"""

import uuid
from typing import Any

from sqlalchemy import Column, ForeignKey, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base
from src.models import uuid_pk

Table(
    "user_account",
    Base.metadata,
    Column("user_id", PGUUID(as_uuid=True), primary_key=True),
    schema="auth",
)
Table(
    "task",
    Base.metadata,
    Column("task_id", PGUUID(as_uuid=True), primary_key=True),
    schema="form",
)
Table(
    "question",
    Base.metadata,
    Column("question_id", PGUUID(as_uuid=True), primary_key=True),
    schema="form",
)


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
