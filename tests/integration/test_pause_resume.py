"""Webhook-level tests for pause/resume (US2-3) -- exercises src.line.router
+ src.conversation.service together through the real /line/webhook route
and a real Postgres session, with Kotlin's forms endpoint and LINE's reply
call mocked at the boundary.
"""

import uuid
from unittest.mock import AsyncMock, patch

import respx
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.forms.config import forms_settings
from src.line.config import line_settings
from tests.integration.helpers import (
    build_form_response,
    build_postback_event,
    build_text_message_event,
    question_json,
    seed_conversation,
    seed_conversation_answer,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)

_PAUSE_LABEL = "⏸️ พักไว้ก่อน"


async def _conversation_row(session: AsyncSession, conversation_id: uuid.UUID) -> dict:
    result = await session.execute(
        text(
            "SELECT status, current_question_id FROM chat.conversation WHERE conversation_id = :id"
        ),
        {"id": conversation_id},
    )
    row = result.mappings().one()
    return dict(row)


async def _send_message(client: AsyncClient, *, line_user_id: str, text_content: str) -> AsyncMock:
    body = build_text_message_event(line_user_id=line_user_id, text_content=text_content)
    signature = sign_signature(body, line_settings.LINE_CHANNEL_SECRET)
    with patch(
        "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
    ) as reply_message:
        response = await client.post(
            "/line/webhook", content=body, headers={"X-Line-Signature": signature}
        )
    assert response.status_code == 200
    return reply_message


async def _send_postback(client: AsyncClient, *, line_user_id: str, data: str) -> AsyncMock:
    body = build_postback_event(line_user_id=line_user_id, data=data)
    signature = sign_signature(body, line_settings.LINE_CHANNEL_SECRET)
    with patch(
        "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
    ) as reply_message:
        response = await client.post(
            "/line/webhook", content=body, headers={"X-Line-Signature": signature}
        )
    assert response.status_code == 200
    return reply_message


class TestExplicitPause:
    async def test_pause_label_pauses_without_touching_answers(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id = await seed_task_form(db_session)
        question_1 = await seed_question(
            db_session, task_id=task_id, field_name="notes", input_type="VARCHAR", sort_order=1
        )
        question_2 = await seed_question(
            db_session,
            task_id=task_id,
            field_name="notes2",
            input_type="VARCHAR",
            label="คำถามที่สอง",
            sort_order=2,
        )
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=question_2,
            status="active",
        )
        await seed_conversation_answer(
            db_session,
            conversation_id=conversation_id,
            question_id=question_1,
            text_value="คำตอบแรก",
        )

        form_response = build_form_response(
            task_form_id=task_form_id,
            questions=[
                question_json(
                    question_id=question_1, field_name="notes", input_type="VARCHAR", sort_order=1
                ),
                question_json(
                    question_id=question_2,
                    field_name="notes2",
                    input_type="VARCHAR",
                    label="คำถามที่สอง",
                    sort_order=2,
                ),
            ],
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _send_message(
                client, line_user_id=line_user_id, text_content=_PAUSE_LABEL
            )

        reply_message.assert_awaited_once()
        reply_text = reply_message.await_args.args[0].messages[0].text
        assert reply_text == (
            'พักงานนี้ไว้ให้แล้ว คำตอบที่ตอบไปแล้วถูกบันทึกครบถ้วน พิมพ์ "เริ่ม" เพื่อดูรายการงานเมื่อพร้อมทำต่อ'
        )

        row = await _conversation_row(db_session, conversation_id)
        assert row["status"] == "paused"
        assert row["current_question_id"] == question_2

        answered = await db_session.execute(
            text(
                "SELECT answer FROM chat.conversation_answer "
                "WHERE conversation_id = :conversation_id AND question_id = :question_id"
            ),
            {"conversation_id": conversation_id, "question_id": question_1},
        )
        assert answered.scalar_one()["text"] == "คำตอบแรก"


class TestResume:
    async def test_resume_shows_recap_then_next_question(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id = await seed_task_form(db_session)
        question_1 = await seed_question(
            db_session, task_id=task_id, field_name="notes", input_type="VARCHAR", sort_order=1
        )
        question_2 = await seed_question(
            db_session,
            task_id=task_id,
            field_name="notes2",
            input_type="VARCHAR",
            label="คำถามที่สอง",
            sort_order=2,
        )
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=question_2,
            status="paused",
        )
        await seed_conversation_answer(
            db_session,
            conversation_id=conversation_id,
            question_id=question_1,
            text_value="คำตอบแรก",
        )

        form_response = build_form_response(
            task_form_id=task_form_id,
            questions=[
                question_json(
                    question_id=question_1, field_name="notes", input_type="VARCHAR", sort_order=1
                ),
                question_json(
                    question_id=question_2,
                    field_name="notes2",
                    input_type="VARCHAR",
                    label="คำถามที่สอง",
                    sort_order=2,
                ),
            ],
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start:{task_id}:{task_form_id}:simple_handler",
            )

        reply_message.assert_awaited_once()
        reply_text = reply_message.await_args.args[0].messages[0].text
        assert reply_text == "คำตอบที่บันทึกไว้:\n- คำถามทดสอบ: คำตอบแรก\n\nคำถามที่สอง"

        row = await _conversation_row(db_session, conversation_id)
        assert row["status"] == "active"
        assert row["current_question_id"] == question_2
