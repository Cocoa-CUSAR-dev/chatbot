"""Conversation-service paths no test reached (docs-and-plan#247).

- Two messages for one conversation at the same moment: the row lock drops
  the second one instead of letting both write an answer.
- The multi-choice picker's less common turns: an old bubble on a paused
  conversation, skipping a question that can't be skipped, typing the skip
  label, re-opening a single-plot answer for editing, and plots narrowed to
  the farm already chosen on the same form.
- Confirming a conversation that was cancelled.

Same harness as the other integration tests here: the real webhook, signed
LINE events, a real Postgres, and Kotlin mocked at the HTTP boundary.
"""

import json
import uuid

import respx
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_maker
from src.forms.config import forms_settings
from tests.integration.helpers import (
    build_form_response,
    question_json,
    seed_conversation,
    seed_farm_with_plots,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
)
from tests.integration.test_multi_choice_flow import (
    _flex_json,
    _seed,
    _send_postback,
    _send_text,
    _sent,
    _start_at_plot_question,
)
from tests.integration.test_non_text_messages import _send as _send_message


def _mock_form(task_form_id: uuid.UUID, form: dict) -> None:
    respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
        return_value=Response(200, json=form)
    )


async def _answers(session: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
    rows = await session.execute(
        text("SELECT answer FROM chat.conversation_answer WHERE conversation_id = :c"),
        {"c": conversation_id},
    )
    return [row[0] for row in rows.all()]


async def _status(session: AsyncSession, conversation_id: uuid.UUID) -> str:
    row = await session.execute(
        text("SELECT status FROM chat.conversation WHERE conversation_id = :c"),
        {"c": conversation_id},
    )
    return str(row.scalar_one())


def _bubble_labels(reply_message) -> list[str]:  # noqa: ANN001
    bubble = _flex_json(reply_message)
    return [
        item["action"]["label"]
        for item in bubble["body"]["contents"]
        if item["type"] == "button" and item["action"].get("data", "").startswith("multi_toggle")
    ]


async def _seed_open_question(
    session: AsyncSession, *, input_type: str
) -> tuple[str, uuid.UUID, uuid.UUID, dict]:
    """A linked farmer mid-way through a one-question form whose question is
    of `input_type`. Returns (line_user_id, conversation_id, task_form_id,
    form)."""
    line_user_id = f"U{uuid.uuid4().hex}"
    user_id = await seed_user_with_line_identity(session, line_user_id=line_user_id)
    task_id, task_form_id = await seed_task_form(session)
    question_id = await seed_question(
        session, task_id=task_id, field_name="gis", input_type=input_type
    )
    form = build_form_response(
        task_form_id=task_form_id,
        questions=[question_json(question_id=question_id, field_name="gis", input_type=input_type)],
    )
    conversation_id = await seed_conversation(
        session,
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        current_question_id=question_id,
    )
    return line_user_id, conversation_id, task_form_id, form


class TestTwoMessagesAtOnce:
    """LINE redelivers, and farmers double-tap. While one message for a
    conversation is being handled (its row is locked), a second one is
    dropped -- first message wins -- rather than both writing an answer."""

    async def test_a_typed_answer_arriving_mid_processing_is_dropped(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id, conversation_id, task_form_id, form = await _seed_open_question(
            db_session, input_type="VARCHAR"
        )

        async with async_session_maker() as in_flight:
            # The first message, still being processed.
            await in_flight.execute(
                text("SELECT 1 FROM chat.conversation WHERE conversation_id = :c FOR UPDATE"),
                {"c": conversation_id},
            )
            with respx.mock:
                _mock_form(task_form_id, form)
                reply = await _send_text(
                    client, line_user_id=line_user_id, text_content="ข้อความที่สอง"
                )
            await in_flight.rollback()

        reply.assert_not_awaited()
        assert await _answers(db_session, conversation_id) == []

    async def test_a_location_arriving_mid_processing_is_dropped(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id, conversation_id, task_form_id, form = await _seed_open_question(
            db_session, input_type="GEODATA"
        )
        location = {
            "type": "location",
            "id": "1",
            "title": "สวน",
            "address": "เชียงใหม่",
            "latitude": 18.79,
            "longitude": 98.98,
        }

        async with async_session_maker() as in_flight:
            await in_flight.execute(
                text("SELECT 1 FROM chat.conversation WHERE conversation_id = :c FOR UPDATE"),
                {"c": conversation_id},
            )
            with respx.mock:
                _mock_form(task_form_id, form)
                reply = await _send_message(client, line_user_id=line_user_id, message=location)
            await in_flight.rollback()

        reply.assert_not_awaited()
        assert await _answers(db_session, conversation_id) == []


class TestPickerEdgeTurns:
    async def test_an_old_bubble_on_a_paused_conversation_changes_nothing(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture, status="paused")

        with respx.mock:
            _mock_form(fixture.task_form_id, fixture.form_response)
            for data in (f"multi_done:{conversation_id}", f"multi_skip:{conversation_id}"):
                reply = await _send_postback(client, line_user_id=fixture.line_user_id, data=data)
                assert _sent(reply).text == "คำถามนี้ผ่านไปแล้วครับ", data

        assert await _status(db_session, conversation_id) == "paused"

    async def test_skipping_a_mandatory_question_is_refused(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The bubble never shows skip on a mandatory question, but a bubble
        sent before the researcher made it mandatory still has the button."""
        fixture = await _seed(db_session, is_mandatory=True)
        conversation_id = await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            _mock_form(fixture.task_form_id, fixture.form_response)
            reply = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"multi_skip:{conversation_id}"
            )

        assert "จำเป็นต้องเลือกอย่างน้อย 1 รายการ" in _sent(reply).alt_text
        answers = await _answers(db_session, conversation_id)
        assert not any(answer.get("skipped") for answer in answers)

    async def test_typing_the_skip_label_skips_the_question(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            _mock_form(fixture.task_form_id, fixture.form_response)
            await _send_text(client, line_user_id=fixture.line_user_id, text_content="ทั้งฟาร์ม")

        assert {"skipped": True} in await _answers(db_session, conversation_id)

    async def test_editing_a_single_plot_answer_shows_it_ticked(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """One plot is stored like any OPTION answer ({"text", "value"});
        re-opening it must not show a blank bubble."""
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        plot_a = fixture.plot_ids[0]
        await db_session.execute(
            text(
                "INSERT INTO chat.conversation_answer (conversation_id, question_id, answer, "
                "source) VALUES (:c, :q, CAST(:a AS jsonb), 'guided_flow')"
            ),
            {
                "c": conversation_id,
                "q": fixture.plot_question_id,
                "a": json.dumps({"text": "แปลง A", "value": str(plot_a)}),
            },
        )
        await db_session.execute(
            text(
                "UPDATE chat.conversation SET current_question_id = NULL WHERE conversation_id = :c"
            ),
            {"c": conversation_id},
        )
        await db_session.commit()

        with respx.mock:
            _mock_form(fixture.task_form_id, fixture.form_response)
            reply = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"edit_pick:{conversation_id}:{fixture.plot_question_id}",
            )

        assert [label for label in _bubble_labels(reply) if label.startswith("✅")] == ["✅ แปลง A"]

    async def test_plots_are_narrowed_to_the_farm_chosen_earlier_on_the_form(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """When the form asked which farm first, offering plots from the
        farmer's OTHER farms would contradict that answer."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        farm_a, _ = await seed_farm_with_plots(
            db_session, farmer_id=user_id, plot_names=["แปลง A1", "แปลง A2"], farm_name="ไร่ A"
        )
        await seed_farm_with_plots(
            db_session, farmer_id=user_id, plot_names=["แปลง B1"], farm_name="ไร่ B"
        )
        task_id, task_form_id = await seed_task_form(
            db_session, handler="farm_activity", is_multiple_submit=True
        )
        farm_question = await seed_question(
            db_session, task_id=task_id, field_name="farm_id", input_type="OPTION", sort_order=1
        )
        plot_question = await seed_question(
            db_session,
            task_id=task_id,
            field_name="plot_id",
            input_type="OPTION",
            is_mandatory=False,
            sort_order=2,
        )
        form = build_form_response(
            task_form_id=task_form_id,
            is_multiple_submit=True,
            questions=[
                question_json(
                    question_id=farm_question,
                    field_name="farm_id",
                    input_type="OPTION",
                    sort_order=1,
                    choices=[{"id": str(farm_a), "name": "ไร่ A"}],
                ),
                question_json(
                    question_id=plot_question,
                    field_name="plot_id",
                    input_type="OPTION",
                    is_mandatory=False,
                    sort_order=2,
                    choices=[{"id": str(uuid.uuid4()), "name": "แปลงของคนอื่น"}],
                ),
            ],
        )
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=plot_question,
            status="paused",
        )
        await db_session.execute(
            text(
                "INSERT INTO chat.conversation_answer (conversation_id, question_id, answer, "
                "source) VALUES (:c, :q, CAST(:a AS jsonb), 'guided_flow')"
            ),
            {
                "c": conversation_id,
                "q": farm_question,
                "a": json.dumps({"text": "ไร่ A", "value": str(farm_a)}),
            },
        )
        await db_session.commit()

        with respx.mock:
            _mock_form(task_form_id, form)
            reply = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start:{task_id}:{task_form_id}:farm_activity",
            )

        assert _bubble_labels(reply) == ["แปลง A1", "แปลง A2"]


class TestConfirmingACancelledConversation:
    async def test_an_old_confirm_button_after_cancelling_sends_nothing(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture, status="cancelled")

        with respx.mock:
            _mock_form(fixture.task_form_id, fixture.form_response)
            reply = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
            )

        assert _sent(reply).text == "รายการนี้ถูกยกเลิกไปแล้วครับ"
        assert await _status(db_session, conversation_id) == "cancelled"
