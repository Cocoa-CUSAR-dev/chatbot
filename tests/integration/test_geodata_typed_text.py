"""Typed text at a GEODATA question.

The 📍 button is the intended way to answer, but a farmer can always type
instead. Pasted coordinates must still reach Go as the [{"lat", "lng"}] list
(never the "lat,lng" string the chatbot reads them as), and anything else
must re-ask with the button rather than dead-end or guess a coordinate.
"""

import uuid
from unittest.mock import AsyncMock

import respx
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.forms.config import forms_settings
from tests.integration.helpers import (
    build_text_message_event,
    seed_conversation,
    seed_conversation_answer,
    seed_user_with_line_identity,
)
from tests.integration.test_geodata_location import (
    _assert_go_accepts,
    _farm_activity_form,
    _post,
    _sent,
    _stored_answer,
)


class TestTypedTextAtAGeodataQuestion:
    async def _ask_gis_then_type(
        self, db_session: AsyncSession, client: AsyncClient, typed: str
    ) -> tuple[AsyncMock, uuid.UUID]:
        task_id, task_form_id, notes_q, gis_q, form_response = await _farm_activity_form(db_session)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=gis_q,
            status="active",
        )
        await seed_conversation_answer(
            db_session, conversation_id=conversation_id, question_id=notes_q, text_value="x"
        )
        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _post(
                client,
                build_text_message_event(line_user_id=line_user_id, text_content=typed),
            )
        return reply_message, gis_q

    async def test_pasted_coordinates_are_stored_as_a_list(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        _, gis_q = await self._ask_gis_then_type(db_session, client, "13.75, 100.5")

        stored = await _stored_answer(db_session, gis_q)
        assert stored["value"] == [{"lat": 13.75, "lng": 100.5}]
        _assert_go_accepts(stored["value"])
        assert stored["text"] == "📍 13.75, 100.5"

    async def test_anything_else_re_asks_with_the_location_button(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Never geocoded, never a dead end: a place name gets the button
        again instead of a guessed coordinate.
        """
        reply_message, gis_q = await self._ask_gis_then_type(db_session, client, "สวนหลังบ้าน")

        message = _sent(reply_message)
        assert "กรุณากดปุ่ม 📍 ส่งตำแหน่ง ด้านล่างครับ" in message.text
        assert "ขออภัยครับ อ่านตำแหน่งจากข้อความนี้ไม่ได้" in message.text
        assert message.quick_reply.items[0].action.type == "location"
        assert await _stored_answer(db_session, gis_q) is None

    async def test_skip_still_skips(self, db_session: AsyncSession, client: AsyncClient) -> None:
        _, gis_q = await self._ask_gis_then_type(db_session, client, "⏭️ ข้าม")

        assert await _stored_answer(db_session, gis_q) == {"skipped": True}
