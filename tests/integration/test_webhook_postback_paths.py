"""Webhook paths the rest of the suite never reached (docs-and-plan#247).

Coverage showed them unexercised end to end, and several are the flows a
farmer hits most: tapping a task with no previous submission, a Go or
sanitizer outage while the reuse offer is being built, the cancel and
"finish" buttons, an unlinked LINE account tapping an old button, and a
button that points at a conversation that no longer exists. Each test
goes through the real webhook with a signed LINE event and a real Postgres,
the same way the other integration tests in this directory do. Only Kotlin
and Go are mocked, at the HTTP boundary.
"""

import uuid
from unittest.mock import AsyncMock, patch

import respx
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.forms.config import forms_settings
from src.line import parent_picker
from src.line.config import line_settings
from src.line.messages import NOT_LINKED
from src.tasks.config import tasks_settings
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

_LAST_ANSWER = f"{tasks_settings.GO_BACKEND_URL}/service/tasks/last-answer"
_SANITIZE = f"{tasks_settings.GO_BACKEND_URL}/service/autofill/sanitize"


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


def _texts(reply_message: AsyncMock) -> list[str]:
    return [
        getattr(m, "text", None) or m.alt_text for m in reply_message.await_args.args[0].messages
    ]


async def _conversations(session: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    result = await session.execute(
        text(
            "SELECT conversation_id, status, current_question_id "
            "FROM chat.conversation WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    return [dict(row) for row in result.mappings().all()]


async def _seed_one_question_task(
    session: AsyncSession, *, handler: str = "notes"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict]:
    """A task with one mandatory VARCHAR question: the form.question row (for
    the FK an answer needs) and the Kotlin-shaped form for respx to return.
    """
    task_id, task_form_id = await seed_task_form(session, handler=handler)
    question_id = await seed_question(
        session, task_id=task_id, field_name="notes", input_type="VARCHAR", label="บันทึกเพิ่มเติม"
    )
    form = build_form_response(
        task_form_id=task_form_id,
        questions=[
            question_json(
                question_id=question_id,
                field_name="notes",
                input_type="VARCHAR",
                label="บันทึกเพิ่มเติม",
            )
        ],
    )
    return task_id, task_form_id, question_id, form


def _mock_form(task_form_id: uuid.UUID, form: dict) -> None:
    respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
        return_value=Response(200, json=form)
    )


class TestStartATask:
    async def test_no_previous_submission_asks_the_first_question(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The everyday path: tap a task never submitted before."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, question_id, form = await _seed_one_question_task(db_session)

        with respx.mock:
            _mock_form(task_form_id, form)
            respx.get(_LAST_ANSWER).mock(return_value=Response(404, json={"error": "none"}))
            reply = await _send_postback(
                client, line_user_id=line_user_id, data=f"start:{task_id}:{task_form_id}:notes"
            )

        assert "บันทึกเพิ่มเติม" in _texts(reply)[0]
        [conversation] = await _conversations(db_session, user_id)
        assert conversation["status"] == "active"
        assert conversation["current_question_id"] == question_id

    async def test_go_down_while_checking_for_a_last_answer_still_starts_the_form(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """A Go outage costs the farmer the reuse offer, never the form."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, question_id, form = await _seed_one_question_task(db_session)

        with respx.mock:
            _mock_form(task_form_id, form)
            respx.get(_LAST_ANSWER).mock(return_value=Response(500, json={"error": "boom"}))
            reply = await _send_postback(
                client, line_user_id=line_user_id, data=f"start:{task_id}:{task_form_id}:notes"
            )

        assert "บันทึกเพิ่มเติม" in _texts(reply)[0]
        [conversation] = await _conversations(db_session, user_id)
        assert conversation["current_question_id"] == question_id

    async def test_sanitizer_down_still_offers_reuse_without_a_preview(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, _, form = await _seed_one_question_task(db_session)

        with respx.mock:
            _mock_form(task_form_id, form)
            respx.get(_LAST_ANSWER).mock(
                return_value=Response(200, json={"answer": {"notes": "ค่าเดิม"}})
            )
            respx.post(_SANITIZE).mock(return_value=Response(500, json={"error": "boom"}))
            reply = await _send_postback(
                client, line_user_id=line_user_id, data=f"start:{task_id}:{task_form_id}:notes"
            )

        offer = _texts(reply)[0]
        assert "ต้องการนำมาใช้กรอกให้อัตโนมัติ" in offer
        assert "ค่าเดิม" not in offer
        # The offer comes before any conversation exists.
        assert await _conversations(db_session, user_id) == []

    async def test_child_form_with_no_parent_to_pick_says_so_and_starts_nothing(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """harvest_grade_detail needs a harvest first; a farmer with none is
        told to record one, and no empty conversation is left behind.

        The parent lookup itself is stubbed: the test schema has no
        collection.harvest, and what's under test is the router's handling
        of "nothing to pick", not the query (tests/line/ covers that)."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, _, form = await _seed_one_question_task(
            db_session, handler="harvest_grade_detail"
        )

        with (
            respx.mock,
            patch("src.line.parent_picker.choices_for", new=AsyncMock(return_value=[])),
        ):
            _mock_form(task_form_id, form)
            reply = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start:{task_id}:{task_form_id}:harvest_grade_detail",
            )

        assert _texts(reply) == [parent_picker.EMPTY_PROMPT["harvest"]]
        assert await _conversations(db_session, user_id) == []


class TestUseLastAnswerButton:
    async def test_yes_with_go_down_starts_a_blank_form(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The farmer said "ใช้ข้อมูลเดิม" but Go can't hand the answer over
        any more: they get the blank form rather than an error."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, question_id, form = await _seed_one_question_task(db_session)

        with respx.mock:
            _mock_form(task_form_id, form)
            respx.get(_LAST_ANSWER).mock(return_value=Response(500, json={"error": "boom"}))
            reply = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start_autofill:yes:{task_id}:{task_form_id}:notes",
            )

        assert "บันทึกเพิ่มเติม" in _texts(reply)[0]
        [conversation] = await _conversations(db_session, user_id)
        assert conversation["current_question_id"] == question_id

    async def test_yes_with_the_sanitizer_down_starts_a_blank_form(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Unsanitized answers are never used: without the sanitizer, nothing
        is prefilled."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, question_id, form = await _seed_one_question_task(db_session)

        with respx.mock:
            _mock_form(task_form_id, form)
            respx.get(_LAST_ANSWER).mock(
                return_value=Response(200, json={"answer": {"notes": "ค่าเดิม"}})
            )
            respx.post(_SANITIZE).mock(return_value=Response(500, json={"error": "boom"}))
            reply = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start_autofill:yes:{task_id}:{task_form_id}:notes",
            )

        assert "บันทึกเพิ่มเติม" in _texts(reply)[0]
        [conversation] = await _conversations(db_session, user_id)
        assert conversation["current_question_id"] == question_id
        answers = await db_session.execute(
            text("SELECT COUNT(*) FROM chat.conversation_answer WHERE conversation_id = :c"),
            {"c": conversation["conversation_id"]},
        )
        assert answers.scalar_one() == 0

    async def test_an_old_offer_resumes_the_conversation_already_started(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Tapping the offer again after the task was already started must not
        open a second conversation for it."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, question_id, form = await _seed_one_question_task(db_session)
        existing = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=question_id,
            status="paused",
        )

        with respx.mock:
            _mock_form(task_form_id, form)
            reply = await _send_postback(
                client,
                line_user_id=line_user_id,
                data=f"start_autofill:no:{task_id}:{task_form_id}:notes",
            )

        assert "บันทึกเพิ่มเติม" in _texts(reply)[-1]
        [conversation] = await _conversations(db_session, user_id)
        assert conversation["conversation_id"] == existing
        assert conversation["status"] == "active"


class TestEndingAConversation:
    async def test_cancel_button_cancels(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, _, _ = await _seed_one_question_task(db_session)
        conversation_id = await seed_conversation(
            db_session, user_id=user_id, task_id=task_id, task_form_id=task_form_id
        )

        reply = await _send_postback(
            client, line_user_id=line_user_id, data=f"cancel:{conversation_id}"
        )

        assert "ยกเลิก" in _texts(reply)[0]
        [conversation] = await _conversations(db_session, user_id)
        assert conversation["status"] == "cancelled"

    async def test_finish_multi_thanks_the_farmer_and_pushes_the_diary(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, _, _ = await _seed_one_question_task(db_session)
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            status="completed",
        )

        push_flex = AsyncMock()
        with (
            patch("src.line.router.generate_diary", new=AsyncMock(return_value="ไดอารี่")),
            patch("src.line.router.mint_sso_token", new=AsyncMock(return_value="tok")),
            patch("src.line.router.push_flex", new=push_flex),
        ):
            reply = await _send_postback(
                client, line_user_id=line_user_id, data=f"finish_multi:{conversation_id}"
            )

        assert _texts(reply) == ["บันทึกข้อมูลครบแล้ว ขอบคุณครับ"]
        push_flex.assert_awaited_once()
        assert push_flex.await_args.args[0] == line_user_id


class TestDiaryCardWhenAPartFails:
    async def test_diary_generation_failing_sends_no_card_but_still_thanks(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, _, _ = await _seed_one_question_task(db_session)
        conversation_id = await seed_conversation(
            db_session, user_id=user_id, task_id=task_id, task_form_id=task_form_id
        )

        push_flex = AsyncMock()
        with (
            patch("src.line.router.generate_diary", new=AsyncMock(side_effect=RuntimeError("llm"))),
            patch("src.line.router.push_flex", new=push_flex),
        ):
            reply = await _send_postback(
                client, line_user_id=line_user_id, data=f"finish_multi:{conversation_id}"
            )

        assert _texts(reply) == ["บันทึกข้อมูลครบแล้ว ขอบคุณครับ"]
        push_flex.assert_not_awaited()

    async def test_sso_failing_still_sends_the_card_with_a_login_link(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """No working deep link: the card goes out anyway, its button landing
        on /history (the login page) instead of signing the farmer in."""
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id, _, _ = await _seed_one_question_task(db_session)
        conversation_id = await seed_conversation(
            db_session, user_id=user_id, task_id=task_id, task_form_id=task_form_id
        )

        push_flex = AsyncMock()
        with (
            patch("src.line.router.generate_diary", new=AsyncMock(return_value="ไดอารี่")),
            patch("src.line.router.mint_sso_token", new=AsyncMock(side_effect=RuntimeError("sso"))),
            patch("src.line.router.push_flex", new=push_flex),
        ):
            await _send_postback(
                client, line_user_id=line_user_id, data=f"finish_multi:{conversation_id}"
            )

        push_flex.assert_awaited_once()
        assert f"{line_settings.WEB_APP_URL}/history" in str(push_flex.await_args.args[2])
        assert "sso#token" not in str(push_flex.await_args.args[2])


class TestButtonsThatNoLongerPointAnywhere:
    async def test_unlinked_account_tapping_a_task_is_told_to_link(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id, _, _ = await _seed_one_question_task(db_session)
        reply = await _send_postback(
            client,
            line_user_id=f"U{uuid.uuid4().hex}",
            data=f"start:{task_id}:{task_form_id}:notes",
        )
        assert _texts(reply) == [NOT_LINKED]

    async def test_unlinked_account_tapping_the_reuse_offer_is_told_to_link(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id, _, _ = await _seed_one_question_task(db_session)
        reply = await _send_postback(
            client,
            line_user_id=f"U{uuid.uuid4().hex}",
            data=f"start_autofill:yes:{task_id}:{task_form_id}:notes",
        )
        assert _texts(reply) == [NOT_LINKED]

    async def test_buttons_for_a_conversation_that_no_longer_exists(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Old buttons stay tappable in the chat forever; one pointing at a
        conversation that's gone gets a plain answer, never an error."""
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        gone = uuid.uuid4()

        for data in (
            f"confirm:{gone}",
            f"edit:{gone}",
            f"edit_pick:{gone}:{uuid.uuid4()}",
            f"add_another:{gone}",
            f"multi_toggle:{gone}:{uuid.uuid4()}",
            f"multi_done:{gone}",
        ):
            reply = await _send_postback(client, line_user_id=line_user_id, data=data)
            assert _texts(reply) == ["ไม่พบบทสนทนานี้แล้ว"], data
