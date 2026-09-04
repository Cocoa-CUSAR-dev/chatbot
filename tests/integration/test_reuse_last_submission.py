"""US2-4 (offer reusing my last submission) against a real Postgres session.

Go itself isn't run here -- its two endpoints (last-answer, autofill/sanitize)
are mocked at the HTTP boundary via respx, same as Kotlin's forms endpoint
already is elsewhere in this test suite. What's worth a REAL DB round-trip
is start_conversation_with_autofill's own writes: it session.add()s multiple
ConversationAnswer rows in one go before a single commit, each with a real
FK to form.question -- exactly the class of bug a mocked session can't
catch (see this repo's own history: the FK-violation live-caught during the
child-handler-501 rollout looked fine against every mock).
"""

import uuid
from unittest.mock import AsyncMock, patch

import respx
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.forms.config import forms_settings
from src.line.config import line_settings
from src.tasks.config import tasks_settings
from tests.integration.helpers import (
    build_form_response,
    build_postback_event,
    question_json,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)


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


def _reply_texts(reply_message: AsyncMock) -> list[str]:
    return [m.text for m in reply_message.await_args.args[0].messages]


async def _conversation_row(session: AsyncSession, *, user_id: uuid.UUID) -> dict:
    result = await session.execute(
        text(
            "SELECT conversation_id, status, current_question_id "
            "FROM chat.conversation WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    return dict(result.mappings().one())


async def _answer_rows(session: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
    result = await session.execute(
        text(
            "SELECT question_id, answer FROM chat.conversation_answer "
            "WHERE conversation_id = :conversation_id"
        ),
        {"conversation_id": conversation_id},
    )
    return [dict(row) for row in result.mappings().all()]


class TestReuseLastSubmission:
    async def test_full_offer_yes_flow_writes_real_prefilled_answer_rows(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        # "notes" -- not one of the 5 parent-picker handlers (see
        # parent_picker._PARENT_KIND_BY_HANDLER), so the autofill offer path
        # is actually reachable rather than short-circuited.
        task_id, task_form_id = await seed_task_form(db_session, handler="notes")
        question_1 = uuid.uuid4()
        question_2 = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO form.question "
                "(question_id, section_id, label, field_name, input_type, sort_order, "
                "is_mandatory) "
                "VALUES (:qid, :sid, :label, :field_name, 'VARCHAR', :sort_order, true)"
            ),
            [
                {
                    "qid": question_1,
                    "sid": uuid.uuid4(),
                    "label": "หมายเหตุ",
                    "field_name": "field_0",
                    "sort_order": 1,
                },
                {
                    "qid": question_2,
                    "sid": uuid.uuid4(),
                    "label": "หมายเหตุที่สอง",
                    "field_name": "field_1",
                    "sort_order": 2,
                },
            ],
        )
        await db_session.commit()

        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        form_response = build_form_response(
            task_form_id=task_form_id,
            questions=[
                question_json(
                    question_id=question_1, field_name="field_0", input_type="VARCHAR", sort_order=1
                ),
                question_json(
                    question_id=question_2,
                    field_name="field_1",
                    input_type="VARCHAR",
                    label="หมายเหตุที่สอง",
                    sort_order=2,
                ),
            ],
        )
        last_answer = {
            "handler": "notes",
            "submitted_at": "2026-08-01T10:00:00Z",
            "answer": {"field_0": "ค่าเดิม 1", "field_1": "ค่าเดิม 2"},
        }
        sanitized = {"answer": {"field_0": "ค่าเดิม 1", "field_1": "ค่าเดิม 2"}}

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            respx.get(f"{tasks_settings.GO_BACKEND_URL}/service/tasks/last-answer").mock(
                return_value=Response(200, json=last_answer)
            )
            respx.post(f"{tasks_settings.GO_BACKEND_URL}/service/autofill/sanitize").mock(
                return_value=Response(200, json=sanitized)
            )

            # 1. "start" -- a prior COMPLETED submission exists, so expect
            # the autofill offer, not straight into the guided flow.
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start:{task_id}:{task_form_id}:notes",
            )
            assert "ใช้ข้อมูลเดิม" in _reply_texts(reply_message)[0] or (
                "ต้องการนำมาใช้กรอกให้อัตโนมัติ" in _reply_texts(reply_message)[0]
            )
            # No conversation should exist yet -- the offer is sent before
            # start_conversation_with_autofill is ever called.
            no_conversation = await db_session.execute(
                text("SELECT COUNT(*) FROM chat.conversation WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            assert no_conversation.scalar_one() == 0

            # 2. Tap "yes" -- expect straight to AWAITING_CONFIRMATION, since
            # the sanitized answer covers every question on the form.
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start_autofill:yes:{task_id}:{task_form_id}:notes",
            )
            summary_text = _reply_texts(reply_message)[0]
            assert "ค่าเดิม 1" in summary_text
            assert "ค่าเดิม 2" in summary_text

        row = await _conversation_row(db_session, user_id=user_id)
        assert row["status"] == "active"
        assert row["current_question_id"] is None

        rows = await _answer_rows(db_session, row["conversation_id"])
        by_question = {r["question_id"]: r["answer"]["text"] for r in rows}
        assert by_question == {question_1: "ค่าเดิม 1", question_2: "ค่าเดิม 2"}
