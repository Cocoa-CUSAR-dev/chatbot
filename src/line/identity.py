"""Read-only lookup against auth.line_identity -- schema owned by the
`database` repo's Flyway migrations (V9__line_chat_notify_models.sql, ADR
0005). Deliberately narrow: only the lookup half (given a LINE user_id,
find the linked auth.user_account.user_id).

The other half -- HOW a row gets into this table in the first place
(pairing code, OAuth, something else) -- is ADR 0002, which the team has
reopened and not yet settled. This module takes no position on that; it
just reads whatever link already exists. See src/line/router.py's
_resolve_user_id, the only caller.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base
from src.models import uuid_pk


class LineIdentity(Base):
    __tablename__ = "line_identity"
    __table_args__ = {"schema": "auth"}

    line_identity_id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column()
    line_user_id: Mapped[str] = mapped_column()
    display_name: Mapped[str | None] = mapped_column(nullable=True)


async def lookup_user_id(session: AsyncSession, line_user_id: str) -> uuid.UUID | None:
    result = await session.execute(
        select(LineIdentity.user_id).where(LineIdentity.line_user_id == line_user_id)
    )
    return result.scalar_one_or_none()
