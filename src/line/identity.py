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
from collections.abc import Sequence

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


async def lookup_line_user_ids(
    session: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The forward direction of lookup_user_id: given internal user_ids, find
    each one's LINE user_id so a proactive push can be addressed to them.

    A user_id with no auth.line_identity row is simply absent from the result
    -- callers decide what to do with the ones they can't reach (see
    src/notifications/service.py, src/reminders/jobs.py).
    """
    if not user_ids:
        return {}
    result = await session.execute(
        select(LineIdentity.user_id, LineIdentity.line_user_id).where(
            LineIdentity.user_id.in_(list(user_ids))
        )
    )
    return {row.user_id: row.line_user_id for row in result}
