"""US2-11 (docs-and-plan#185) -- the first thing a farmer ever sees.

Adding the OA as a friend used to produce a log line and nothing else: no
reply, no explanation of what the bot is for. Webhook-level because the fix
is in how that event type is handled, and because "linked or not" is a real
DB lookup.
"""

import uuid
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.line.config import line_settings
from tests.integration.helpers import (
    build_follow_event,
    seed_user_with_line_identity,
    sign_signature,
)


async def _follow(client: AsyncClient, *, line_user_id: str) -> AsyncMock:
    body = build_follow_event(line_user_id=line_user_id)
    signature = sign_signature(body, line_settings.LINE_CHANNEL_SECRET)
    with patch(
        "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
    ) as reply_message:
        response = await client.post(
            "/line/webhook", content=body, headers={"X-Line-Signature": signature}
        )
    assert response.status_code == 200
    return reply_message


class TestFollowWelcome:
    async def test_linked_farmer_gets_a_welcome_and_a_start_button(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        message = (await _follow(client, line_user_id=line_user_id)).await_args.args[0].messages[0]

        assert "ยินดีต้อนรับ" in message.text
        assert [item.action.text for item in message.quick_reply.items] == ["เริ่ม"]

    async def test_unlinked_farmer_is_pointed_at_a_researcher(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """ADR 0002 is undecided, so the copy must not invent a linking
        procedure -- it names a human instead.
        """
        message = (
            (await _follow(client, line_user_id=f"U{uuid.uuid4().hex}"))
            .await_args.args[0]
            .messages[0]
        )

        assert "ยังไม่ได้เชื่อมกับบัญชีในระบบ" in message.text
        assert "นักวิจัยประจำแปลง" in message.text
        assert message.quick_reply is None
