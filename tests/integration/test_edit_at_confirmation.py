"""US2-6 ("แก้ไข", edit-at-confirmation) against a real Postgres session --
the piece worth a real DB round-trip here specifically is handle_answer's
upsert (service.py): editing re-opens an already-answered question, and the
new answer must UPDATE that row in place, not insert a second one for the
same question_id. That's exactly the kind of thing a mocked session can't
prove -- SQLAlchemy's change tracking on a JSONB column needs the attribute
reassigned (not mutated) to flush an UPDATE, and only a real commit+re-query
actually confirms it happened.
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


async def _conversation_row(session: AsyncSession, conversation_id: uuid.UUID) -> dict:
    result = await session.execute(
        text(
            "SELECT status, current_question_id FROM chat.conversation WHERE conversation_id = :id"
        ),
        {"id": conversation_id},
    )
    return dict(result.mappings().one())


async def _answer_rows(
    session: AsyncSession, conversation_id: uuid.UUID, question_id: uuid.UUID
) -> list[dict]:
    result = await session.execute(
        text(
            "SELECT answer FROM chat.conversation_answer "
            "WHERE conversation_id = :conversation_id AND question_id = :question_id"
        ),
        {"conversation_id": conversation_id, "question_id": question_id},
    )
    return [dict(row) for row in result.mappings().all()]


def _reply_texts(reply_message: AsyncMock) -> list[str]:
    return [m.text for m in reply_message.await_args.args[0].messages]


class TestEditAtConfirmation:
    async def test_full_edit_flow_updates_the_answer_row_in_place(
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
        # Already at AWAITING_CONFIRMATION -- both questions answered,
        # current_question_id NULL, exactly what start_conversation/
        # handle_answer leave behind once every question is filled.
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=None,
            status="active",
        )
        await seed_conversation_answer(
            db_session, conversation_id=conversation_id, question_id=question_1, text_value="เดิม 1"
        )
        await seed_conversation_answer(
            db_session, conversation_id=conversation_id, question_id=question_2, text_value="เดิม 2"
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

            # 1. Tap "แก้ไข" -- expect a picker listing both answered questions.
            reply_message = await _send_postback(
                client, line_user_id=line_user_id, data=f"edit:{conversation_id}"
            )
            picker_text = _reply_texts(reply_message)[0]
            assert "เลือกข้อที่ต้องการแก้ไข" in picker_text

            # 2. Pick question_2 -- expect it re-asked, exactly like a fresh
            # guided-flow question.
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"edit_pick:{conversation_id}:{question_2}",
            )
            assert _reply_texts(reply_message)[0] == "คำถามที่สอง"

            row = await _conversation_row(db_session, conversation_id)
            assert row["current_question_id"] == question_2

            # 3. Answer it with a NEW value -- expect straight back to
            # AWAITING_CONFIRMATION (every question is already answered) with
            # the updated value in the summary.
            reply_message = await _send_message(
                client, line_user_id=line_user_id, text_content="ใหม่ 2"
            )
            summary_text = _reply_texts(reply_message)[0]
            assert "ใหม่ 2" in summary_text
            assert "เดิม 2" not in summary_text  # old value isn't still showing
            assert "เดิม 1" in summary_text  # the untouched question is unaffected

        # The real assertion this test exists for: exactly ONE row for
        # question_2, holding the NEW value -- not two rows (a stale one
        # plus a new one) that _format_answered_lines/confirm_conversation
        # would otherwise both have to somehow reconcile.
        rows = await _answer_rows(db_session, conversation_id, question_2)
        assert len(rows) == 1
        assert rows[0]["answer"]["text"] == "ใหม่ 2"

        row = await _conversation_row(db_session, conversation_id)
        assert row["status"] == "active"
        assert row["current_question_id"] is None  # back at AWAITING_CONFIRMATION
