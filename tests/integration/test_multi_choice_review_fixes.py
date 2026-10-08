"""Regression tests for the three bugs found in review of chatbot#76.

1. Two ยืนยัน taps (or a LINE redelivery) sent every plot twice -- 3 plots
   became 6 rows. confirm_conversation didn't lock or check status.
2. After a partial save, แก้ไข + a fresh selection reset `submitted`, so the
   next confirm re-sent the plots already saved.
3. An old ยืนยัน tapped mid-selection submitted the half-built answer --
   plot_id missing, i.e. a whole-farm record nobody chose.
4. The fix for (3) re-showed the open step through resume_conversation,
   which set a PAUSED conversation ACTIVE again (docs-and-plan#222).

Real Postgres on purpose: (1) is a race between two webhook requests and
only a real row lock can serialise them, and all three are about state an
EARLIER request wrote.
"""

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import respx
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.forms.config import forms_settings
from src.line.config import line_settings
from tests.integration.helpers import build_postback_event, sign_signature
from tests.integration.test_multi_choice_flow import (
    _Fixture,
    _seed,
    _send_postback,
    _sent,
    _start_at_plot_question,
)


async def _park_at_confirmation(
    db_session: AsyncSession,
    fixture: _Fixture,
    *,
    submitted: list[str] | None = None,
) -> uuid.UUID:
    """A conversation sitting on the summary with all three plots chosen
    (and, optionally, some of them already saved by an earlier confirm).
    """
    conversation_id = await _start_at_plot_question(db_session, fixture)
    values = [str(plot_id) for plot_id in fixture.plot_ids]
    answer: dict[str, Any] = {
        "text": "แปลง A, แปลง B, แปลง C",
        "values": values,
        "labels": ["แปลง A", "แปลง B", "แปลง C"],
        "multi": True,
        "submitted": submitted or [],
    }
    await db_session.execute(
        text(
            "INSERT INTO chat.conversation_answer (conversation_id, question_id, answer, source) "
            "VALUES (:cid, :qid, CAST(:answer AS jsonb), 'guided_flow')"
        ),
        {"cid": conversation_id, "qid": fixture.plot_question_id, "answer": json.dumps(answer)},
    )
    await db_session.execute(
        text("UPDATE chat.conversation SET current_question_id = NULL WHERE conversation_id = :c"),
        {"c": conversation_id},
    )
    await db_session.commit()
    return conversation_id


def _mock_form(fixture: _Fixture) -> None:
    respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}").mock(
        return_value=Response(200, json=fixture.form_response)
    )


async def _status(db_session: AsyncSession, conversation_id: uuid.UUID) -> str:
    row = await db_session.execute(
        text("SELECT status FROM chat.conversation WHERE conversation_id = :c"),
        {"c": conversation_id},
    )
    return str(row.scalar_one())


def _submitted_plots(submit_task: AsyncMock) -> list[str]:
    return [call.args[0].answer.get("plot_id") for call in submit_task.await_args_list]


class TestDoubleConfirmNeverDuplicates:
    async def test_two_simultaneous_confirms_write_each_plot_once(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The race itself: both requests in flight at once. Each Go call is
        slowed down so the second request genuinely arrives while the first
        is still mid-submit -- before the fix, both read submitted=[].
        """
        fixture = await _seed(db_session)
        conversation_id = await _park_at_confirmation(db_session, fixture)

        async def slow_submit(_submission: Any) -> None:
            await asyncio.sleep(0.2)

        submit_task = AsyncMock(side_effect=slow_submit)
        body_a = build_postback_event(
            line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
        )
        body_b = build_postback_event(
            line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
        )
        with (
            respx.mock,
            patch("src.conversation.service.submit_task", new=submit_task),
            patch(
                "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
            ) as reply_message,
            patch("src.line.router.generate_diary", new=AsyncMock(return_value="ไดอารี่")),
            patch("src.line.router.push_flex", new=AsyncMock()),
            patch("src.line.router.mint_sso_token", new=AsyncMock(return_value="token")),
        ):
            _mock_form(fixture)
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/line/webhook",
                        content=body,
                        headers={
                            "X-Line-Signature": sign_signature(
                                body, line_settings.LINE_CHANNEL_SECRET
                            )
                        },
                    )
                    for body in (body_a, body_b)
                )
            )

        assert all(response.status_code == 200 for response in responses)
        assert sorted(_submitted_plots(submit_task)) == sorted(
            str(plot_id) for plot_id in fixture.plot_ids
        ), "every plot must be sent exactly once"
        replies = [call.args[0].messages[0] for call in reply_message.await_args_list]
        texts = [getattr(m, "text", None) or m.alt_text for m in replies]
        assert any("บันทึกข้อมูลชุดนี้ไปแล้วครับ" in t for t in texts)
        assert await _status(db_session, conversation_id) == "completed"

    async def test_a_confirm_after_completion_sends_nothing(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The sequential version (e.g. LINE redelivering a postback later)."""
        fixture = await _seed(db_session)
        conversation_id = await _park_at_confirmation(db_session, fixture)
        submit_task = AsyncMock()

        with respx.mock, patch("src.conversation.service.submit_task", new=submit_task):
            _mock_form(fixture)
            await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
            )
            second = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
            )

        assert submit_task.await_count == 3
        assert _sent(second).text == "บันทึกข้อมูลชุดนี้ไปแล้วครับ"


class TestNoEditAfterAPartialSave:
    async def test_edit_is_refused_and_confirm_sends_only_the_rest(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The reviewer's sequence: plot A saved, B and C not; farmer taps
        แก้ไข. Editing would reset `submitted` and re-send A. Now แก้ไข is
        refused with the confirm buttons, and the retry sends B and C only.
        """
        fixture = await _seed(db_session)
        plot_a = str(fixture.plot_ids[0])
        conversation_id = await _park_at_confirmation(db_session, fixture, submitted=[plot_a])
        submit_task = AsyncMock()

        with respx.mock, patch("src.conversation.service.submit_task", new=submit_task):
            _mock_form(fixture)
            edit = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"edit:{conversation_id}"
            )
            # An old question-picker button for the same conversation.
            stale_pick = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"edit_pick:{conversation_id}:{fixture.plot_question_id}",
            )
            await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
            )

        edit_message = _sent(edit)
        assert "แก้ไขคำตอบไม่ได้" in edit_message.text
        assert any("ยืนยัน" in item.action.label for item in edit_message.quick_reply.items)
        assert "แก้ไขคำตอบไม่ได้" in _sent(stale_pick).text
        assert plot_a not in _submitted_plots(submit_task), "plot A was already saved"
        assert sorted(_submitted_plots(submit_task)) == sorted(
            str(plot_id) for plot_id in fixture.plot_ids[1:]
        )
        assert await _status(db_session, conversation_id) == "completed"


class TestStaleConfirmMidSelection:
    async def test_an_old_confirm_button_never_submits_a_half_built_selection(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Mid-selection (one plot ticked, not finished), an old ยืนยัน from
        an earlier summary must not send plot_id=None -- a whole-farm row.
        """
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        await db_session.execute(
            text(
                "INSERT INTO chat.conversation_answer "
                "(conversation_id, question_id, answer, source) "
                "VALUES (:cid, :qid, CAST(:answer AS jsonb), 'guided_flow')"
            ),
            {
                "cid": conversation_id,
                "qid": fixture.plot_question_id,
                "answer": json.dumps(
                    {"selecting": True, "values": [str(fixture.plot_ids[0])], "labels": ["แปลง A"]}
                ),
            },
        )
        await db_session.commit()
        submit_task = AsyncMock()

        with respx.mock, patch("src.conversation.service.submit_task", new=submit_task):
            _mock_form(fixture)
            reply_message = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
            )

        submit_task.assert_not_awaited()
        assert _sent(reply_message).alt_text  # the picker bubble is re-shown
        assert await _status(db_session, conversation_id) == "active"

    async def test_an_old_confirm_button_leaves_a_paused_conversation_paused(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """docs-and-plan#222: re-showing the open step used to go through
        resume_conversation, which set a PAUSED conversation back to ACTIVE --
        alongside whatever task the farmer had started since pausing it.
        """
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        await db_session.execute(
            text("UPDATE chat.conversation SET status = 'paused' WHERE conversation_id = :c"),
            {"c": conversation_id},
        )
        await db_session.commit()
        submit_task = AsyncMock()

        with respx.mock, patch("src.conversation.service.submit_task", new=submit_task):
            _mock_form(fixture)
            reply_message = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
            )

        submit_task.assert_not_awaited()
        assert "พักไว้" in _sent(reply_message).text
        assert await _status(db_session, conversation_id) == "paused"
