"""Multi-submit Phase 1 (chatbot half) against a real Postgres session.

Two things need a real database rather than a mock:

1. The pending-task picker is raw SQL. Its old `LEFT JOIN form.response ...
   WHERE r.response_id IS NULL` dropped a task the moment one response
   existed, which is exactly what made the second grade row unreachable --
   and the naive fix (keep the join, add an OR) would have listed the task
   once per response instead. Only a real query proves both.
2. start_next_submission writes a new chat.conversation row carrying the
   previous one's resolved parent_answer forward, with a real FK to
   form.question on current_question_id.
"""

import uuid
from unittest.mock import AsyncMock, patch

import respx
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.forms.config import forms_settings
from src.line.config import line_settings
from src.line.temp_task_picker import list_pending_tasks
from tests.integration.helpers import (
    build_form_response,
    build_postback_event,
    question_json,
    seed_conversation,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)


async def _seed_response(session: AsyncSession, *, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await session.execute(
        text("INSERT INTO form.response (task_log_id, user_id) VALUES (:task_id, :user_id)"),
        {"task_id": task_id, "user_id": user_id},
    )
    await session.commit()


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


class TestPickerKeepsMultiSubmitTasksListed:
    async def test_single_submit_task_disappears_once_submitted(
        self, db_session: AsyncSession
    ) -> None:
        """The existing behaviour, which must not change."""
        task_id, _ = await seed_task_form(db_session, is_multiple_submit=False)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        before = await list_pending_tasks(db_session, user_id)
        assert task_id in {t.task_id for t in before}

        await _seed_response(db_session, task_id=task_id, user_id=user_id)

        after = await list_pending_tasks(db_session, user_id)
        assert task_id not in {t.task_id for t in after}

    async def test_multi_submit_task_stays_listed_after_submitting(
        self, db_session: AsyncSession
    ) -> None:
        task_id, _ = await seed_task_form(db_session, is_multiple_submit=True)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        await _seed_response(db_session, task_id=task_id, user_id=user_id)

        tasks = await list_pending_tasks(db_session, user_id)
        assert task_id in {t.task_id for t in tasks}, (
            "a multi-submit task must stay listed after the first submission, "
            "or the second row is unreachable"
        )

    async def test_multi_submit_task_is_listed_once_not_once_per_response(
        self, db_session: AsyncSession
    ) -> None:
        """The trap in the cheap version of this fix: keeping the LEFT JOIN
        and adding an OR would list the task once per response row.
        """
        task_id, _ = await seed_task_form(db_session, is_multiple_submit=True)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        for _ in range(3):
            await _seed_response(db_session, task_id=task_id, user_id=user_id)

        tasks = await list_pending_tasks(db_session, user_id)
        appearances = [t for t in tasks if t.task_id == task_id]
        assert len(appearances) == 1, f"listed {len(appearances)} times, want exactly 1"

    async def test_another_farmers_responses_dont_hide_my_task(
        self, db_session: AsyncSession
    ) -> None:
        task_id, _ = await seed_task_form(db_session, is_multiple_submit=False)
        mine = f"U{uuid.uuid4().hex}"
        theirs = f"U{uuid.uuid4().hex}"
        my_user_id = await seed_user_with_line_identity(db_session, line_user_id=mine)
        their_user_id = await seed_user_with_line_identity(db_session, line_user_id=theirs)

        await _seed_response(db_session, task_id=task_id, user_id=their_user_id)

        tasks = await list_pending_tasks(db_session, my_user_id)
        assert task_id in {t.task_id for t in tasks}


class TestAddAnotherCarriesParentForward:
    async def test_next_submission_reuses_the_resolved_parent(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The whole reason start_next_submission exists: the farmer picked
        a harvest once, and the second grade row must not re-ask for it.
        """
        task_id, task_form_id = await seed_task_form(
            db_session, handler="harvest_grade_detail", is_multiple_submit=True
        )
        question_id = await seed_question(
            db_session,
            task_id=task_id,
            field_name="grade_code",
            input_type="VARCHAR",
            label="เกรด",
            sort_order=1,
        )
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        parent_answer = {"field_name": "harvest_id", "value": str(uuid.uuid4())}
        completed_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=None,
            status="completed",
            parent_answer=parent_answer,
        )

        form_response = build_form_response(
            task_form_id=task_form_id,
            questions=[
                question_json(
                    question_id=question_id,
                    field_name="grade_code",
                    input_type="VARCHAR",
                    label="เกรด",
                    sort_order=1,
                )
            ],
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _send_postback(
                client, line_user_id=line_user_id, data=f"add_another:{completed_id}"
            )

        # Straight to the form's first real question -- no parent picker.
        sent = reply_message.await_args.args[0].messages[0].text
        assert sent == "เกรด", f"expected the first question, got {sent!r}"

        rows = await db_session.execute(
            text(
                "SELECT conversation_id, status, parent_answer, current_question_id "
                "FROM chat.conversation WHERE user_id = :user_id AND status = 'active'"
            ),
            {"user_id": user_id},
        )
        fresh = rows.mappings().all()
        assert len(fresh) == 1, "expected exactly one new active conversation"
        assert fresh[0]["conversation_id"] != completed_id, "must be a NEW conversation"
        assert fresh[0]["parent_answer"] == parent_answer, (
            "parent selection was not carried forward"
        )
        assert fresh[0]["current_question_id"] == question_id

    async def test_unresolved_parent_picker_state_is_not_carried_forward(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """A conversation still sitting on {"pending_kind": ...} never picked
        a parent -- copying that would start the next submission stuck at
        the picker step.
        """
        task_id, task_form_id = await seed_task_form(
            db_session, handler="harvest_grade_detail", is_multiple_submit=True
        )
        question_id = await seed_question(
            db_session,
            task_id=task_id,
            field_name="grade_code",
            input_type="VARCHAR",
            label="เกรด",
            sort_order=1,
        )
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        completed_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=None,
            status="completed",
            parent_answer={"pending_kind": "harvest"},
        )

        form_response = build_form_response(
            task_form_id=task_form_id,
            questions=[
                question_json(
                    question_id=question_id,
                    field_name="grade_code",
                    input_type="VARCHAR",
                    label="เกรด",
                    sort_order=1,
                )
            ],
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            await _send_postback(
                client, line_user_id=line_user_id, data=f"add_another:{completed_id}"
            )

        rows = await db_session.execute(
            text(
                "SELECT parent_answer FROM chat.conversation "
                "WHERE user_id = :user_id AND status = 'active'"
            ),
            {"user_id": user_id},
        )
        assert rows.scalar_one() is None, "a pending_kind placeholder must not be copied forward"
