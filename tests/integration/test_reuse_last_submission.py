"""US2-4 (offer reusing my last submission) and US2-6 (edit at confirmation,
where the edited field happens to be one that was autofilled rather than
typed) against a real Postgres session.

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
    build_text_message_event,
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


async def _seed_two_question_form(
    session: AsyncSession, *, task_form_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, dict]:
    """Two mandatory VARCHAR questions ("หมายเหตุ" / "หมายเหตุที่สอง") -- both
    real form.question rows (for the FK a ConversationAnswer write needs)
    and the matching Kotlin-shaped form_response dict (for respx to hand
    back) -- labels come from the latter, the DB row is FK-satisfaction only.
    """
    question_1 = uuid.uuid4()
    question_2 = uuid.uuid4()
    await session.execute(
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
    await session.commit()

    form_response = build_form_response(
        task_form_id=task_form_id,
        questions=[
            question_json(
                question_id=question_1,
                field_name="field_0",
                input_type="VARCHAR",
                label="หมายเหตุ",
                sort_order=1,
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
    return question_1, question_2, form_response


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
            offer_text = _reply_texts(reply_message)[0]
            assert "ต้องการนำมาใช้กรอกให้อัตโนมัติ" in offer_text
            # The preview itself -- a farmer should see what's being offered
            # before agreeing, not just find out after tapping "ใช้ข้อมูลเดิม".
            assert "ค่าเดิม 1" in offer_text
            assert "ค่าเดิม 2" in offer_text
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


class TestAutofillDecline:
    async def test_declining_the_offer_starts_completely_blank(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id = await seed_task_form(db_session, handler="notes")
        question_1, _question_2, form_response = await _seed_two_question_form(
            db_session, task_form_id=task_form_id
        )

        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

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

            # 1. "start" -- same offer as the accept-flow test above;
            # declining is a choice made AFTER seeing exactly the same
            # preview.
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start:{task_id}:{task_form_id}:notes",
            )
            offer_text = _reply_texts(reply_message)[0]
            assert "ค่าเดิม 1" in offer_text

            # 2. Tap "กรอกใหม่" -- expect the plain guided flow's first
            # question, not a confirmation summary and not a recap prefix.
            # fetch_last_answer/sanitize_for_autofill must NOT be consulted
            # again on this path -- start_conversation (not
            # start_conversation_with_autofill) is the only call this
            # branch makes.
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start_autofill:no:{task_id}:{task_form_id}:notes",
            )
            first_question_text = _reply_texts(reply_message)[0]
            assert "หมายเหตุ" in first_question_text
            assert "ค่าเดิม" not in first_question_text  # no leftover recap/prefill wording

        row = await _conversation_row(db_session, user_id=user_id)
        assert row["status"] == "active"
        assert row["current_question_id"] == question_1  # first question, asked fresh

        # The real point of this test: an all-or-nothing decline means
        # literally nothing carried over, not just that the first question
        # LOOKS unprefilled.
        rows = await _answer_rows(db_session, row["conversation_id"])
        assert rows == []


class TestEditingAnAutofilledField:
    async def test_editing_a_reused_field_updates_it_in_place(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id = await seed_task_form(db_session, handler="notes")
        question_1, question_2, form_response = await _seed_two_question_form(
            db_session, task_form_id=task_form_id
        )

        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

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

            # 1-2. Offer, then accept -- straight to AWAITING_CONFIRMATION
            # with both fields prefilled (same as TestReuseLastSubmission's
            # accept-flow test above).
            await _send_postback(
                client, line_user_id=line_user_id, data=f"start:{task_id}:{task_form_id}:notes"
            )
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start_autofill:yes:{task_id}:{task_form_id}:notes",
            )
            summary_text = _reply_texts(reply_message)[0]
            assert "ค่าเดิม 1" in summary_text
            assert "ค่าเดิม 2" in summary_text

            row = await _conversation_row(db_session, user_id=user_id)
            conversation_id = row["conversation_id"]

            # 3. Tap "แก้ไข" -- both autofilled questions must be offered,
            # exactly like a directly-typed answer would be
            # (build_answer_rows's whole point: a prefilled field is
            # indistinguishable from a typed one downstream).
            reply_message = await _send_postback(
                client, line_user_id=line_user_id, data=f"edit:{conversation_id}"
            )
            picker_text = _reply_texts(reply_message)[0]
            assert "เลือกข้อที่ต้องการแก้ไข" in picker_text

            # 4. Pick question_2 (an autofilled field, not typed this turn)
            # -- expect it re-asked like a fresh guided-flow question.
            reply_message = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"edit_pick:{conversation_id}:{question_2}",
            )
            assert _reply_texts(reply_message)[0] == "หมายเหตุที่สอง"

            # 5. Answer with a NEW value -- expect straight back to
            # AWAITING_CONFIRMATION, updated value shown, question_1's
            # own autofilled value untouched.
            reply_message = await _send_message(
                client, line_user_id=line_user_id, text_content="ใหม่ 2"
            )
            final_summary = _reply_texts(reply_message)[0]
            assert "ใหม่ 2" in final_summary
            assert "ค่าเดิม 2" not in final_summary  # old autofilled value isn't still showing
            assert "ค่าเดิม 1" in final_summary  # the untouched autofilled field is unaffected

        # The real assertion: exactly ONE row for question_2 holding the NEW
        # value -- not a stale autofilled row plus a second one from the
        # edit -- and question_1's autofilled row is completely untouched.
        rows = await _answer_rows(db_session, conversation_id)
        by_question = {r["question_id"]: r["answer"]["text"] for r in rows}
        assert by_question == {question_1: "ค่าเดิม 1", question_2: "ใหม่ 2"}
