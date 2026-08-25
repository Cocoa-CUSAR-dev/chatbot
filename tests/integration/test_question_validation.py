"""Webhook-level tests for the guided-flow answer loop -- exercises
src.line.router + src.conversation.service + a real Postgres session
together through the actual /line/webhook route. Kotlin's forms endpoint is
mocked with respx (it's httpx-based); LINE's reply call is mocked by
patching AsyncMessagingApi.reply_message directly, since line-bot-sdk sends
it over aiohttp, which respx cannot intercept.
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
    build_text_message_event,
    question_json,
    seed_active_conversation,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)


async def _current_question_id(
    session: AsyncSession, conversation_id: uuid.UUID
) -> uuid.UUID | None:
    result = await session.execute(
        text("SELECT current_question_id FROM chat.conversation WHERE conversation_id = :id"),
        {"id": conversation_id},
    )
    return result.scalar_one()


class TestGuidedFlowRoundTrip:
    async def test_valid_answer_advances_via_webhook(
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
        conversation_id = await seed_active_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=question_1,
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

        body = build_text_message_event(line_user_id=line_user_id, text_content="คำตอบที่ถูกต้อง")
        signature = sign_signature(body, line_settings.LINE_CHANNEL_SECRET)

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            with patch(
                "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
            ) as reply_message:
                response = await client.post(
                    "/line/webhook",
                    content=body,
                    headers={"X-Line-Signature": signature},
                )

        assert response.status_code == 200
        reply_message.assert_awaited_once()
        sent_request = reply_message.await_args.args[0]
        assert sent_request.messages[0].text == "คำถามที่สอง"

        assert await _current_question_id(db_session, conversation_id) == question_2

        answered = await db_session.execute(
            text(
                "SELECT answer FROM chat.conversation_answer "
                "WHERE conversation_id = :conversation_id AND question_id = :question_id"
            ),
            {"conversation_id": conversation_id, "question_id": question_1},
        )
        assert answered.scalar_one()["text"] == "คำตอบที่ถูกต้อง"


class TestReAskLoop:
    _QUANTITY_RULE = {
        "type": "FLOAT",
        "min": 0,
        "max": 5000,
        "errorMessage": "กรุณากรอกปริมาณ 0-5,000 (กก.)",
    }

    async def _seed(
        self, session: AsyncSession, *, line_user_id: str
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        task_id, task_form_id = await seed_task_form(session)
        question_1 = await seed_question(
            session, task_id=task_id, field_name="quantity_kg", input_type="FLOAT"
        )
        question_2 = await seed_question(
            session,
            task_id=task_id,
            field_name="notes2",
            input_type="VARCHAR",
            label="คำถามที่สอง",
            sort_order=2,
        )
        user_id = await seed_user_with_line_identity(session, line_user_id=line_user_id)
        conversation_id = await seed_active_conversation(
            session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=question_1,
        )
        return task_form_id, conversation_id, question_1, question_2

    def _mock_form(
        self, *, task_form_id: uuid.UUID, question_1: uuid.UUID, question_2: uuid.UUID
    ) -> None:
        respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
            return_value=Response(
                200,
                json=build_form_response(
                    task_form_id=task_form_id,
                    questions=[
                        question_json(
                            question_id=question_1,
                            field_name="quantity_kg",
                            input_type="FLOAT",
                            validation_rule=self._QUANTITY_RULE,
                        ),
                        question_json(
                            question_id=question_2,
                            field_name="notes2",
                            input_type="VARCHAR",
                            label="คำถามที่สอง",
                            sort_order=2,
                        ),
                    ],
                ),
            )
        )

    async def _send_answer(
        self, client: AsyncClient, *, line_user_id: str, text_content: str
    ) -> AsyncMock:
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

    async def test_invalid_answer_reasks_then_valid_answer_advances(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        task_form_id, conversation_id, question_1, question_2 = await self._seed(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            self._mock_form(task_form_id=task_form_id, question_1=question_1, question_2=question_2)

            reply_message = await self._send_answer(
                client, line_user_id=line_user_id, text_content="abc"
            )
            reply_message.assert_awaited_once()
            error_text = reply_message.await_args.args[0].messages[0].text
            assert "กรุณากรอกปริมาณ" in error_text
            assert await _current_question_id(db_session, conversation_id) == question_1

            reply_message = await self._send_answer(
                client, line_user_id=line_user_id, text_content="120"
            )
            reply_message.assert_awaited_once()
            next_text = reply_message.await_args.args[0].messages[0].text
            assert next_text == "คำถามที่สอง"
            assert await _current_question_id(db_session, conversation_id) == question_2

        answered = await db_session.execute(
            text("SELECT COUNT(*) FROM chat.conversation_answer WHERE conversation_id = :id"),
            {"id": conversation_id},
        )
        assert answered.scalar_one() == 1

    async def test_repeated_invalid_answers_do_not_advance(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        task_form_id, conversation_id, question_1, question_2 = await self._seed(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            self._mock_form(task_form_id=task_form_id, question_1=question_1, question_2=question_2)

            for bad_answer in ("abc", "-5", "99999"):
                reply_message = await self._send_answer(
                    client, line_user_id=line_user_id, text_content=bad_answer
                )
                reply_message.assert_awaited_once()
                assert await _current_question_id(db_session, conversation_id) == question_1

        answered = await db_session.execute(
            text("SELECT COUNT(*) FROM chat.conversation_answer WHERE conversation_id = :id"),
            {"id": conversation_id},
        )
        assert answered.scalar_one() == 0
