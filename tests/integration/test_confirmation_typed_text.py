"""docs-and-plan#189 -- typed text at the confirmation summary, against a
real Postgres session.

Found in manual testing: a farmer typed "เค" at the summary instead of
tapping ยืนยัน and got "ไม่พบบทสนทนานี้แล้ว". The summary's three buttons are
postback-only, so the text fell through to handle_answer, which raises
ConversationNotFound when current_question_id is NULL -- the conversation was
alive the whole time.

A real DB is what makes these worth running: the thing under test is whether
a typed word reaches the *same* code path as the button, which is only
visible in what actually happened to the chat.conversation row (COMPLETED vs
CANCELLED vs untouched) after the webhook returned.
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
    seed_conversation,
    seed_conversation_answer,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)


async def _send_message(client: AsyncClient, *, line_user_id: str, text_content: str) -> AsyncMock:
    """Sends one text event and returns the patched reply_message mock.

    generate_diary/push_flex are patched out for the same reason every other
    outbound call is: the confirm path awaits the diary (chatbot#71), and a
    real one would try to reach Kotlin.
    """
    body = build_text_message_event(line_user_id=line_user_id, text_content=text_content)
    signature = sign_signature(body, line_settings.LINE_CHANNEL_SECRET)
    with (
        patch(
            "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
        ) as reply_message,
        patch("src.line.router.generate_diary", new=AsyncMock(return_value="ไดอารี่")),
        patch("src.line.router.push_flex", new=AsyncMock()),
        patch("src.line.router.mint_sso_token", new=AsyncMock(return_value="token")),
    ):
        response = await client.post(
            "/line/webhook", content=body, headers={"X-Line-Signature": signature}
        )
    assert response.status_code == 200
    return reply_message


def _sent_text(reply_message: AsyncMock) -> str:
    message = reply_message.await_args.args[0].messages[0]
    # The terminal ack is a FlexMessage, whose altText carries the same
    # wording the farmer sees in their notification.
    return str(getattr(message, "text", None) or message.alt_text)


async def _seed_conversation_at_confirmation(
    db_session: AsyncSession, *, line_user_id: str, is_multiple_submit: bool = False
) -> tuple[uuid.UUID, uuid.UUID, dict]:
    """A conversation parked exactly where the bug was found: ACTIVE, every
    question answered, current_question_id NULL -- i.e. the summary is on
    screen and only the three buttons are meant to move it forward.
    """
    task_id, task_form_id = await seed_task_form(
        db_session, handler="notes", is_multiple_submit=is_multiple_submit
    )
    question_id = await seed_question(
        db_session,
        task_id=task_id,
        field_name="notes",
        input_type="VARCHAR",
        label="หมายเหตุ",
        sort_order=1,
    )
    user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
    conversation_id = await seed_conversation(
        db_session,
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        current_question_id=None,
        status="active",
    )
    await seed_conversation_answer(
        db_session,
        conversation_id=conversation_id,
        question_id=question_id,
        text_value="ฝนตก",
    )
    form_response = build_form_response(
        task_form_id=task_form_id,
        is_multiple_submit=is_multiple_submit,
        questions=[
            question_json(
                question_id=question_id,
                field_name="notes",
                input_type="VARCHAR",
                label="หมายเหตุ",
                sort_order=1,
            )
        ],
    )
    return conversation_id, task_form_id, form_response


async def _status_of(db_session: AsyncSession, conversation_id: uuid.UUID) -> str:
    row = await db_session.execute(
        text("SELECT status FROM chat.conversation WHERE conversation_id = :cid"),
        {"cid": conversation_id},
    )
    return str(row.scalar_one())


class TestTypedYesWordConfirms:
    async def test_typed_yes_word_submits_like_the_button(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """ "เค" is the exact word that produced "ไม่พบบทสนทนานี้แล้ว" in manual
        testing."""
        line_user_id = f"U{uuid.uuid4().hex}"
        conversation_id, task_form_id, form_response = await _seed_conversation_at_confirmation(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            with patch("src.conversation.service.submit_task", new=AsyncMock()):
                reply_message = await _send_message(
                    client, line_user_id=line_user_id, text_content="เค"
                )

        sent = _sent_text(reply_message)
        assert "ไม่พบบทสนทนานี้แล้ว" not in sent
        assert "บันทึกข้อมูลเรียบร้อยแล้ว" in sent
        assert await _status_of(db_session, conversation_id) == "completed"

    async def test_politeness_particle_still_counts_as_yes(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        conversation_id, task_form_id, form_response = await _seed_conversation_at_confirmation(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            with patch("src.conversation.service.submit_task", new=AsyncMock()):
                await _send_message(client, line_user_id=line_user_id, text_content="ยืนยันครับ")

        assert await _status_of(db_session, conversation_id) == "completed"

    async def test_multi_submit_form_offers_another_row(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Typing yes must reach the same multi-submit branch the button does
        -- the whole reason both callers share one helper.
        """
        line_user_id = f"U{uuid.uuid4().hex}"
        _, task_form_id, form_response = await _seed_conversation_at_confirmation(
            db_session, line_user_id=line_user_id, is_multiple_submit=True
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            with patch("src.conversation.service.submit_task", new=AsyncMock()):
                reply_message = await _send_message(
                    client, line_user_id=line_user_id, text_content="ตกลง"
                )

        sent_message = reply_message.await_args.args[0].messages[0]
        assert "ต้องการเพิ่มอีกรายการ" in sent_message.text
        labels = [item.action.label for item in sent_message.quick_reply.items]
        assert any("เพิ่มอีกรายการ" in label for label in labels)


class TestTypedCancelAndEdit:
    async def test_typed_cancel_cancels(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        conversation_id, task_form_id, form_response = await _seed_conversation_at_confirmation(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            await _send_message(client, line_user_id=line_user_id, text_content="ยกเลิก")

        assert await _status_of(db_session, conversation_id) == "cancelled"

    async def test_typed_edit_shows_the_edit_picker(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        conversation_id, task_form_id, form_response = await _seed_conversation_at_confirmation(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _send_message(
                client, line_user_id=line_user_id, text_content="แก้ไข"
            )

        sent_message = reply_message.await_args.args[0].messages[0]
        assert "เลือกข้อที่ต้องการแก้ไข" in sent_message.text
        # Nothing was submitted or thrown away by looking at the picker.
        assert await _status_of(db_session, conversation_id) == "active"


class TestUnrecognisedTextIsNeverADeadEnd:
    async def test_junk_text_reshows_the_summary_with_its_buttons(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        conversation_id, task_form_id, form_response = await _seed_conversation_at_confirmation(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _send_message(
                client, line_user_id=line_user_id, text_content="asdfgh"
            )

        sent_message = reply_message.await_args.args[0].messages[0]
        assert "ไม่พบบทสนทนานี้แล้ว" not in sent_message.text
        assert "รบกวนกดปุ่มด้านล่าง" in sent_message.text
        assert "สรุปคำตอบของคุณ" in sent_message.text
        assert "หมายเหตุ: ฝนตก" in sent_message.text
        labels = [item.action.label for item in sent_message.quick_reply.items]
        assert any("ยืนยัน" in label for label in labels)
        # Still exactly where it was -- re-showing the summary changes nothing.
        assert await _status_of(db_session, conversation_id) == "active"

    async def test_typed_pause_still_pauses(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Pausing is valid from ANY step, including this one -- the typed
        pause label must keep reaching handle_answer rather than being
        treated as unrecognised text.
        """
        line_user_id = f"U{uuid.uuid4().hex}"
        conversation_id, task_form_id, form_response = await _seed_conversation_at_confirmation(
            db_session, line_user_id=line_user_id
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            await _send_message(client, line_user_id=line_user_id, text_content="⏸️ พักไว้ก่อน")

        assert await _status_of(db_session, conversation_id) == "paused"
