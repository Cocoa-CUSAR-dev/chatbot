"""The multi-plot picker end to end, against a real Postgres and a mocked Go.

Webhook-level because almost everything that can go wrong here is about state
that survives between taps: the selection lives in a chat.conversation_answer
row, each tap is its own webhook request with its own session, and the
stale-bubble guard reads the conversation row that an earlier request wrote.
A mocked session proves none of that.

Built on tests/integration/test_multi_submit.py's fixtures, since a
multiple-submit form is a precondition of this feature.
"""

import json
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
    seed_farm_with_plots,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)


async def _send_postback(client: AsyncClient, *, line_user_id: str, data: str) -> AsyncMock:
    body = build_postback_event(line_user_id=line_user_id, data=data)
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


async def _send_text(client: AsyncClient, *, line_user_id: str, text_content: str) -> AsyncMock:
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


def _sent(reply_message: AsyncMock):  # noqa: ANN202
    return reply_message.await_args.args[0].messages[0]


def _flex_json(reply_message: AsyncMock) -> dict:
    """The bubble as plain JSON, so a test can read the buttons the farmer
    actually sees rather than the SDK's model objects.
    """
    message = _sent(reply_message)
    return json.loads(message.contents.to_json())


def _postback_data(bubble: dict) -> list[str]:
    body = bubble["body"]["contents"] + bubble.get("footer", {}).get("contents", [])
    return [item["action"].get("data", "") for item in body if item["type"] == "button"]


class _Fixture:
    def __init__(
        self,
        *,
        line_user_id: str,
        user_id: uuid.UUID,
        task_id: uuid.UUID,
        task_form_id: uuid.UUID,
        plot_question_id: uuid.UUID,
        plot_ids: list[uuid.UUID],
        form_response: dict,
    ) -> None:
        self.line_user_id = line_user_id
        self.user_id = user_id
        self.task_id = task_id
        self.task_form_id = task_form_id
        self.plot_question_id = plot_question_id
        self.plot_ids = plot_ids
        self.form_response = form_response


async def _seed(
    db_session: AsyncSession,
    *,
    is_multiple_submit: bool = True,
    plot_names: list[str] | None = None,
    is_mandatory: bool = False,
) -> _Fixture:
    """A farmer with plots, parked on the plot question of a multiple-submit
    "จดกิจกรรมในสวน"-shaped form (a free-text note, then the plot).
    """
    names = plot_names if plot_names is not None else ["แปลง A", "แปลง B", "แปลง C"]
    line_user_id = f"U{uuid.uuid4().hex}"
    user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
    _, plot_ids = await seed_farm_with_plots(db_session, farmer_id=user_id, plot_names=names)

    task_id, task_form_id = await seed_task_form(
        db_session, handler="farm_activity", is_multiple_submit=is_multiple_submit
    )
    note_question_id = await seed_question(
        db_session,
        task_id=task_id,
        field_name="description",
        input_type="VARCHAR",
        label="กิจกรรมที่ทำ",
        sort_order=1,
    )
    plot_question_id = await seed_question(
        db_session,
        task_id=task_id,
        field_name="plot_id",
        input_type="OPTION",
        label="แปลงที่ดำเนินการ",
        is_mandatory=is_mandatory,
        sort_order=2,
    )
    form_response = build_form_response(
        task_form_id=task_form_id,
        is_multiple_submit=is_multiple_submit,
        questions=[
            question_json(
                question_id=note_question_id,
                field_name="description",
                input_type="VARCHAR",
                label="กิจกรรมที่ทำ",
                sort_order=1,
            ),
            question_json(
                question_id=plot_question_id,
                field_name="plot_id",
                input_type="OPTION",
                label="แปลงที่ดำเนินการ",
                is_mandatory=is_mandatory,
                sort_order=2,
                # The global ref.plot_constant list Kotlin really sends --
                # deliberately NOT the farmer's own plots, so a test that
                # passes can only have used the scoped picker query.
                choices=[{"id": str(uuid.uuid4()), "name": "แปลงของคนอื่น"}],
            ),
        ],
    )
    return _Fixture(
        line_user_id=line_user_id,
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        plot_question_id=plot_question_id,
        plot_ids=plot_ids,
        form_response=form_response,
    )


async def _start_at_plot_question(
    db_session: AsyncSession, fixture: _Fixture, *, status: str = "active"
) -> uuid.UUID:
    conversation_id = await seed_conversation(
        db_session,
        user_id=fixture.user_id,
        task_id=fixture.task_id,
        task_form_id=fixture.task_form_id,
        current_question_id=fixture.plot_question_id,
        status=status,
    )
    await db_session.execute(
        text(
            "INSERT INTO chat.conversation_answer "
            "(conversation_id, question_id, answer, source) "
            "SELECT :cid, question_id, CAST(:answer AS jsonb), 'guided_flow' "
            "FROM form.question WHERE field_name = 'description' AND question_id IN ("
            "  SELECT question_id FROM form.question WHERE field_name = 'description'"
            ") LIMIT 1"
        ),
        {"cid": conversation_id, "answer": json.dumps({"text": "พ่นยา"})},
    )
    await db_session.commit()
    return conversation_id


async def _answer_rows(db_session: AsyncSession, conversation_id: uuid.UUID) -> dict:
    rows = await db_session.execute(
        text(
            "SELECT q.field_name, a.answer FROM chat.conversation_answer a "
            "JOIN form.question q ON q.question_id = a.question_id "
            "WHERE a.conversation_id = :cid"
        ),
        {"cid": conversation_id},
    )
    return {row.field_name: row.answer for row in rows}


class TestThePickerIsShown:
    async def test_the_plot_question_comes_back_as_a_bubble_of_the_farmers_own_plots(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await seed_conversation(
            db_session,
            user_id=fixture.user_id,
            task_id=fixture.task_id,
            task_form_id=fixture.task_form_id,
            current_question_id=fixture.plot_question_id,
            status="paused",
        )

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            reply_message = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"start:{fixture.task_id}:{fixture.task_form_id}:farm_activity",
            )

        bubble = _flex_json(reply_message)
        labels = [
            item["action"]["label"]
            for item in bubble["body"]["contents"]
            if item["type"] == "button"
        ]
        assert labels == ["แปลง A", "แปลง B", "แปลง C"]
        # Kotlin's global plot list never appears -- these are the scoped ones.
        assert "แปลงของคนอื่น" not in labels
        assert any(f"multi_done:{conversation_id}" in data for data in _postback_data(bubble))

    async def test_a_non_multi_submit_form_still_uses_quick_reply(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The untouched path: one row per submission means one plot."""
        fixture = await _seed(db_session, is_multiple_submit=False)
        await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            reply_message = await _send_text(
                client, line_user_id=fixture.line_user_id, text_content="อะไรก็ได้"
            )

        message = _sent(reply_message)
        assert message.quick_reply is not None  # Quick Reply, not a Flex bubble

    async def test_a_farmer_with_one_plot_still_uses_quick_reply(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session, plot_names=["แปลงเดียว"])
        await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            reply_message = await _send_text(
                client, line_user_id=fixture.line_user_id, text_content="อะไรก็ได้"
            )

        assert _sent(reply_message).quick_reply is not None


class TestSelecting:
    async def test_tap_toggle_and_done_then_confirm_writes_one_row_per_plot(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The whole feature in one test: pick A and C, change your mind about
        C, add B, finish, confirm -- two rows, identical but for the plot.
        """
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        plot_a, plot_b, plot_c = fixture.plot_ids

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))

            await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_a}",
            )
            reply_message = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_c}",
            )
            assert "เลือกแล้ว: แปลง A, แปลง C (2 รายการ)" in _sent(reply_message).text

            # Tapping C again removes it -- the farmer changed their mind.
            reply_message = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_c}",
            )
            assert "เลือกแล้ว: แปลง A (1 รายการ)" in _sent(reply_message).text

            await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_b}",
            )
            reply_message = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"multi_done:{conversation_id}"
            )
            assert (
                "จะบันทึกเป็น 2 รายการ (แยกตามแปลงที่ดำเนินการ): แปลง A, แปลง B"
                in _sent(reply_message).text
            )

            submit = AsyncMock()
            with patch("src.conversation.service.submit_task", new=submit):
                reply_message = await _send_postback(
                    client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
                )

        submissions = [call.args[0].answer for call in submit.await_args_list]
        assert submissions == [
            {"description": "พ่นยา", "plot_id": str(plot_a)},
            {"description": "พ่นยา", "plot_id": str(plot_b)},
        ]
        assert "2 รายการ" in _sent(reply_message).text
        stored = await _answer_rows(db_session, conversation_id)
        assert stored["plot_id"]["submitted"] == [str(plot_a), str(plot_b)]

    async def test_done_with_nothing_selected_re_asks(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            reply_message = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"multi_done:{conversation_id}"
            )

        assert "กรุณาเลือกอย่างน้อย 1 รายการ" in _sent(reply_message).alt_text

    async def test_one_plot_is_stored_exactly_like_a_normal_answer(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        plot_a = fixture.plot_ids[0]

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_a}",
            )
            await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"multi_done:{conversation_id}"
            )

            submit = AsyncMock()
            with patch("src.conversation.service.submit_task", new=submit):
                await _send_postback(
                    client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
                )

        stored = await _answer_rows(db_session, conversation_id)
        assert stored["plot_id"] == {"text": "แปลง A", "value": str(plot_a)}
        assert submit.await_count == 1

    async def test_whole_farm_skips_the_question(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_skip:{conversation_id}",
            )

            submit = AsyncMock()
            with patch("src.conversation.service.submit_task", new=submit):
                await _send_postback(
                    client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
                )

        stored = await _answer_rows(db_session, conversation_id)
        assert stored["plot_id"] == {"skipped": True}
        assert submit.await_args.args[0].answer == {"description": "พ่นยา"}


class TestStaleBubble:
    async def test_tapping_an_old_bubble_after_the_question_moved_on_changes_nothing(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """A sent Flex message can never be retracted or edited, so its
        buttons stay live forever -- including after the farmer has answered.
        """
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        plot_a, plot_b, _ = fixture.plot_ids

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_a}",
            )
            await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"multi_done:{conversation_id}"
            )

            reply_message = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_b}",
            )

        assert _sent(reply_message).text == "คำถามนี้ผ่านไปแล้วครับ"
        stored = await _answer_rows(db_session, conversation_id)
        assert stored["plot_id"] == {"text": "แปลง A", "value": str(plot_a)}

    async def test_a_plot_that_is_not_this_farmers_is_refused(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Postback data is attacker-controllable in principle, and an id
        stored here would be sent to Go later as if the farmer owned it.
        """
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        someone_else = await seed_user_with_line_identity(
            db_session, line_user_id=f"U{uuid.uuid4().hex}"
        )
        _, other_plots = await seed_farm_with_plots(
            db_session, farmer_id=someone_else, plot_names=["ของคนอื่น"], farm_name="ไร่อื่น"
        )

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            reply_message = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{other_plots[0]}",
            )

        assert _sent(reply_message).text == "คำถามนี้ผ่านไปแล้วครับ"
        assert "plot_id" not in await _answer_rows(db_session, conversation_id)


class TestTypedTextWhileSelecting:
    async def test_typing_a_plot_name_toggles_it(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            reply_message = await _send_text(
                client, line_user_id=fixture.line_user_id, text_content="แปลง B"
            )

        assert "เลือกแล้ว: แปลง B (1 รายการ)" in _sent(reply_message).text
        stored = await _answer_rows(db_session, conversation_id)
        assert stored["plot_id"]["values"] == [str(fixture.plot_ids[1])]

    async def test_typing_done_finishes_the_selection(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{fixture.plot_ids[0]}",
            )
            reply_message = await _send_text(
                client, line_user_id=fixture.line_user_id, text_content="เสร็จ"
            )

        assert "สรุปคำตอบของคุณ" in _sent(reply_message).text

    async def test_unrecognised_text_re_sends_the_picker_instead_of_dead_ending(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            reply_message = await _send_text(
                client, line_user_id=fixture.line_user_id, text_content="asdfgh"
            )

        assert "กรุณากดเลือกจากปุ่มด้านบนครับ" in _sent(reply_message).alt_text
        assert "plot_id" not in await _answer_rows(db_session, conversation_id)


class TestPauseAndResume:
    async def test_resuming_mid_selection_shows_the_bubble_with_the_selection_intact(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        plot_a = fixture.plot_ids[0]

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"multi_toggle:{conversation_id}:{plot_a}",
            )
            await _send_text(client, line_user_id=fixture.line_user_id, text_content="⏸️ พักไว้ก่อน")
            reply_message = await _send_postback(
                client,
                line_user_id=fixture.line_user_id,
                data=f"start:{fixture.task_id}:{fixture.task_form_id}:farm_activity",
            )

        bubble = _flex_json(reply_message)
        selected = [
            item["action"]["label"]
            for item in bubble["body"]["contents"]
            if item["type"] == "button" and item["action"]["label"].startswith("✅")
        ]
        assert selected == ["✅ แปลง A"]


class TestPartialFailureRetry:
    async def test_a_retry_after_a_partial_failure_never_duplicates_a_saved_plot(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The data-integrity case, through the real webhook path: Go accepts
        plot A, fails on B, and the farmer taps ยืนยัน again.
        """
        fixture = await _seed(db_session)
        conversation_id = await _start_at_plot_question(db_session, fixture)
        plot_a, plot_b, _ = fixture.plot_ids

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=fixture.form_response))
            for plot_id in (plot_a, plot_b):
                await _send_postback(
                    client,
                    line_user_id=fixture.line_user_id,
                    data=f"multi_toggle:{conversation_id}:{plot_id}",
                )
            await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"multi_done:{conversation_id}"
            )

            failing = AsyncMock(side_effect=[None, RuntimeError("go is down")])
            with patch("src.conversation.service.submit_task", new=failing):
                reply_message = await _send_postback(
                    client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
                )
            assert "บันทึกแล้ว 1 จาก 2 รายการ" in _sent(reply_message).text

            status = await db_session.execute(
                text("SELECT status FROM chat.conversation WHERE conversation_id = :cid"),
                {"cid": conversation_id},
            )
            assert status.scalar_one() == "active"

            retry = AsyncMock()
            with patch("src.conversation.service.submit_task", new=retry):
                await _send_postback(
                    client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
                )

        retried_plots = [call.args[0].answer["plot_id"] for call in retry.await_args_list]
        assert retried_plots == [str(plot_b)], "the retry must not re-send plot A"
        status = await db_session.execute(
            text("SELECT status FROM chat.conversation WHERE conversation_id = :cid"),
            {"cid": conversation_id},
        )
        assert status.scalar_one() == "completed"


async def _seed_activity_type_form(db_session: AsyncSession) -> tuple[_Fixture, uuid.UUID, dict]:
    """A multiple-submit form with an ordinary OPTION question (the activity
    type, choices straight from Kotlin) BEFORE the plot question -- the shape
    that proves the picker is not plot-only.
    """
    line_user_id = f"U{uuid.uuid4().hex}"
    user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
    _, plot_ids = await seed_farm_with_plots(
        db_session, farmer_id=user_id, plot_names=["แปลง A", "แปลง B"]
    )
    task_id, task_form_id = await seed_task_form(
        db_session, handler="farm_activity", is_multiple_submit=True
    )
    type_question_id = await seed_question(
        db_session,
        task_id=task_id,
        field_name="farm_activity_type_id",
        input_type="OPTION",
        label="กิจกรรมที่ทำ",
        sort_order=1,
    )
    plot_question_id = await seed_question(
        db_session,
        task_id=task_id,
        field_name="plot_id",
        input_type="OPTION",
        label="แปลงที่ดำเนินการ",
        is_mandatory=False,
        sort_order=2,
    )
    activity_choices = {"spray": "พ่นยา", "fertilise": "ใส่ปุ๋ย", "prune": "ตัดแต่งกิ่ง"}
    form_response = build_form_response(
        task_form_id=task_form_id,
        is_multiple_submit=True,
        questions=[
            question_json(
                question_id=type_question_id,
                field_name="farm_activity_type_id",
                input_type="OPTION",
                label="กิจกรรมที่ทำ",
                sort_order=1,
                choices=[{"id": key, "name": name} for key, name in activity_choices.items()],
            ),
            question_json(
                question_id=plot_question_id,
                field_name="plot_id",
                input_type="OPTION",
                label="แปลงที่ดำเนินการ",
                is_mandatory=False,
                sort_order=2,
                choices=[{"id": str(uuid.uuid4()), "name": "แปลงของคนอื่น"}],
            ),
        ],
    )
    fixture = _Fixture(
        line_user_id=line_user_id,
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        plot_question_id=plot_question_id,
        plot_ids=plot_ids,
        form_response=form_response,
    )
    conversation_id = await seed_conversation(
        db_session,
        user_id=user_id,
        task_id=task_id,
        task_form_id=task_form_id,
        current_question_id=type_question_id,
        status="active",
    )
    return fixture, conversation_id, form_response


class TestAnyOptionQuestion:
    async def test_a_non_plot_question_fans_out_by_its_own_choices(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The plot was only the example. Tick two activity types, answer the
        plot once, confirm -- two rows, split by activity type.
        """
        fixture, conversation_id, form_response = await _seed_activity_type_form(db_session)
        plot_a = fixture.plot_ids[0]

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=form_response))

            for choice_id in ("spray", "fertilise"):
                await _send_postback(
                    client,
                    line_user_id=fixture.line_user_id,
                    data=f"multi_toggle:{conversation_id}:{choice_id}",
                )
            reply_message = await _send_postback(
                client, line_user_id=fixture.line_user_id, data=f"multi_done:{conversation_id}"
            )

            # One multi-answer question per round: the activity type already
            # holds two answers, so the plot question is the ordinary
            # one-answer Quick Reply, not a second picker.
            plot_message = _sent(reply_message)
            assert plot_message.quick_reply is not None
            assert getattr(plot_message, "contents", None) is None

            await _send_text(client, line_user_id=fixture.line_user_id, text_content="แปลงของคนอื่น")

            submit = AsyncMock()
            with patch("src.conversation.service.submit_task", new=submit):
                await _send_postback(
                    client, line_user_id=fixture.line_user_id, data=f"confirm:{conversation_id}"
                )

        sent_types = [
            call.args[0].answer["farm_activity_type_id"] for call in submit.await_args_list
        ]
        assert sent_types == ["spray", "fertilise"]
        plot_values = {call.args[0].answer["plot_id"] for call in submit.await_args_list}
        assert len(plot_values) == 1, "every row carries the same single plot answer"
        assert plot_a is not None

    async def test_the_picker_shows_the_questions_own_choices(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        fixture, conversation_id, form_response = await _seed_activity_type_form(db_session)

        with respx.mock:
            respx.get(
                f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{fixture.task_form_id}"
            ).mock(return_value=Response(200, json=form_response))
            reply_message = await _send_text(
                client, line_user_id=fixture.line_user_id, text_content="อะไรนะ"
            )

        bubble = _flex_json(reply_message)
        labels = [
            item["action"]["label"]
            for item in bubble["body"]["contents"]
            if item["type"] == "button"
        ]
        assert labels == ["พ่นยา", "ใส่ปุ๋ย", "ตัดแต่งกิ่ง"]
        # A mandatory question has no skip button on the bubble.
        footer_data = [item["action"].get("data", "") for item in bubble["footer"]["contents"]]
        assert not any(data.startswith("multi_skip:") for data in footer_data)
        assert conversation_id is not None
