"""ORM models for auth.line_identity / auth.line_link_code -- schema owned by
the `database` repo's Flyway migrations (ADR 0005), NOT created or altered
from here. These classes only describe the existing tables for querying.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base
from src.models import uuid_pk


class LineIdentity(Base):
    __tablename__ = "line_identity"
    __table_args__ = {"schema": "auth"}

    line_identity_id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth.user_account.user_id"))
    line_user_id: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class LineLinkCode(Base):
    __tablename__ = "line_link_code"
    __table_args__ = {"schema": "auth"}

    link_code_id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auth.user_account.user_id"))
    code: Mapped[str] = mapped_column(String(6))
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(nullable=True)
