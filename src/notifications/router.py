from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_session
from src.notifications.dependencies import require_service_key
from src.notifications.schemas import SendNotificationRequest, SendNotificationResponse
from src.notifications.service import send_notification

router = APIRouter(prefix="/service", tags=["notifications"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/notifications",
    response_model=SendNotificationResponse,
    dependencies=[Depends(require_service_key)],
)
async def send_notifications(
    body: SendNotificationRequest,
    session: SessionDep,
) -> SendNotificationResponse:
    """Proactive push to one or more users, addressed by user_id (resolved to
    their linked LINE account here). Partial success: user_ids with no linked
    LINE account come back under `failed`; everyone else is still delivered.
    First-party callers only -- gated by the X-Service-Key header.
    """
    return await send_notification(session, body.user_ids, body.message)
