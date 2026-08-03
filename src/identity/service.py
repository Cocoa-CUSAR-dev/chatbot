"""Pairing-code linking (ADR 0002) -- chosen over in-app OAuth/"Login with LINE"
because it's a smaller Flutter footprint and matches patterns this user base
is more likely to already know (Discord phone-linking, smart-TV pairing).
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.identity.exceptions import InvalidLinkCode
from src.identity.models import LineIdentity, LineLinkCode


async def verify_and_link(session: AsyncSession, line_user_id: str, code: str) -> str:
    """Returns the linked user_id, or raises InvalidLinkCode."""
    result = await session.execute(
        select(LineLinkCode).where(LineLinkCode.code == code, LineLinkCode.used_at.is_(None))
    )
    link_code = result.scalar_one_or_none()

    if link_code is None or link_code.expires_at < datetime.now(UTC):
        raise InvalidLinkCode

    link_code.used_at = datetime.now(UTC)
    session.add(
        LineIdentity(
            user_id=link_code.user_id,
            line_user_id=line_user_id,
            linked_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return str(link_code.user_id)
