"""#186 item 2 -- a sticker, photo or location used to be logged and dropped,
so the farmer got no reply at all and no way to tell the bot from a dead one.

Webhook-level rather than unit, because the thing being fixed is precisely
what LINE's own parser hands the router for these types: the branch that
matched them already existed, it just never replied.
"""

import uuid
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.line.config import line_settings
from tests.integration.helpers import (
    build_message_event,
    seed_conversation,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)

# quoteToken is required by the SDK's own model for both of these (it is not
# optional the way it is on a text message), so LINE always sends one.
_IMAGE = {
    "type": "image",
    "id": "1000",
    "contentProvider": {"type": "line"},
    "quoteToken": "q-image",
}
_STICKER = {
    "type": "sticker",
    "id": "1001",
    "packageId": "446",
    "stickerId": "1988",
    "stickerResourceType": "STATIC",
    "quoteToken": "q-sticker",
}
_LOCATION = {
    "type": "location",
    "id": "1002",
    "title": "แปลงหลังบ้าน",
    "address": "เชียงใหม่",
    "latitude": 18.79,
    "longitude": 98.98,
}


async def _send(client: AsyncClient, *, line_user_id: str, message: dict) -> AsyncMock:
    body = build_message_event(line_user_id=line_user_id, message=message)
    signature = sign_signature(body, line_settings.LINE_CHANNEL_SECRET)
    with patch(
        "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
    ) as reply_message:
        response = await client.post(
            "/line/webhook", content=body, headers={"X-Line-Signature": signature}
        )
    assert response.status_code == 200
    return reply_message


class TestNonTextMessagesAlwaysGetAReply:
    async def test_image_gets_a_reply_with_the_start_hint(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        reply_message = await _send(client, line_user_id=line_user_id, message=_IMAGE)

        message = reply_message.await_args.args[0].messages[0]
        assert "ข้อความตัวอักษร" in message.text
        assert [item.action.text for item in message.quick_reply.items] == ["เริ่ม"]

    async def test_sticker_gets_a_reply(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Stickers hit the catch-all branch, which had no reply at all."""
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        reply_message = await _send(client, line_user_id=line_user_id, message=_STICKER)

        assert "ข้อความตัวอักษร" in reply_message.await_args.args[0].messages[0].text

    async def test_mid_form_the_farmer_is_told_to_type_the_answer(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Pointing someone back at the task list mid-form would be wrong --
        they are being asked something specific right now.
        """
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id = await seed_task_form(db_session)
        question_id = await seed_question(
            db_session, task_id=task_id, field_name="notes", input_type="VARCHAR"
        )
        await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=question_id,
            status="active",
        )

        reply_message = await _send(client, line_user_id=line_user_id, message=_LOCATION)

        message = reply_message.await_args.args[0].messages[0]
        assert "กรุณาพิมพ์คำตอบเป็นข้อความ" in message.text
        assert message.quick_reply is None

    async def test_unlinked_user_is_told_so(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        reply_message = await _send(client, line_user_id=f"U{uuid.uuid4().hex}", message=_IMAGE)

        assert "ยังไม่ได้เชื่อมกับบัญชีในระบบ" in reply_message.await_args.args[0].messages[0].text
