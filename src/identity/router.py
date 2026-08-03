from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_session
from src.identity.schemas import LinkCodeVerifyRequest, LinkResult
from src.identity.service import verify_and_link

router = APIRouter(prefix="/identity", tags=["identity"])


@router.post("/link", response_model=LinkResult)
async def link_line_account(
    payload: LinkCodeVerifyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LinkResult:
    user_id = await verify_and_link(session, payload.line_user_id, payload.code)
    return LinkResult(linked=True, user_id=user_id)
