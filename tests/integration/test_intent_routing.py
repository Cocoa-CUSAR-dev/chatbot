"""US2-11 (docs-and-plan#184) -- free text now routes to an existing flow.

Webhook-level with a real DB, because the two rules worth protecting are both
about WHERE the message went, not what the classifier thought: the exact
keyword must never spend an LLM call, and a message arriving mid-form must
never be classified at all (a real answer like "เริ่มเก็บเกี่ยววันนี้" would
otherwise be read as a command and the farmer's answer lost).

intent.classify itself is unit-tested in tests/conversation/test_intent.py;
here it is patched so each test states one classifier verdict and checks the
routing that follows from it.
"""

import uuid
from unittest.mock import AsyncMock, patch

import respx
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.conversation import intent
from src.forms.config import forms_settings
from src.line.config import line_settings
from tests.integration.helpers import (
    build_form_response,
    build_text_message_event,
    question_json,
    seed_conversation,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)


def _verdict(
    value: intent.Intent, *, task_hint: str | None = None, confidence: float = 0.9
) -> AsyncMock:
    return AsyncMock(
        return_value=intent.IntentResult(intent=value, task_hint=task_hint, confidence=confidence)
    )


async def _send(
    client: AsyncClient, *, line_user_id: str, text_content: str, classify: AsyncMock
) -> AsyncMock:
    body = build_text_message_event(line_user_id=line_user_id, text_content=text_content)
    signature = sign_signature(body, line_settings.LINE_CHANNEL_SECRET)
    with (
        patch(
            "src.line.service.AsyncMessagingApi.reply_message", new_callable=AsyncMock
        ) as reply_message,
        patch("src.line.router.intent.classify", new=classify),
    ):
        response = await client.post(
            "/line/webhook", content=body, headers={"X-Line-Signature": signature}
        )
    assert response.status_code == 200
    return reply_message


def _message(reply_message: AsyncMock):  # noqa: ANN202
    return reply_message.await_args.args[0].messages[0]


class TestShowTasks:
    async def test_free_text_reaches_the_task_list(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        await seed_task_form(db_session, title="บันทึกการเก็บเกี่ยว")

        classify = _verdict(intent.Intent.SHOW_TASKS)
        reply_message = await _send(
            client, line_user_id=line_user_id, text_content="ขอทำฟอร์มหน่อย", classify=classify
        )

        message = _message(reply_message)
        assert message.text == "เลือกงานที่ต้องการทำ:"
        assert [item.action.label for item in message.quick_reply.items] == ["บันทึกการเก็บเกี่ยว"]
        # Titles are the only farmer data the classifier is given.
        assert classify.await_args.args[1] == ["บันทึกการเก็บเกี่ยว"]
        assert user_id is not None

    async def test_a_named_task_narrows_the_list_to_it(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        await seed_task_form(db_session, title="บันทึกการเก็บเกี่ยว")
        await seed_task_form(db_session, title="ตรวจโรคและแมลง")

        reply_message = await _send(
            client,
            line_user_id=line_user_id,
            text_content="อยากกรอกเก็บเกี่ยว",
            classify=_verdict(intent.Intent.SHOW_TASKS, task_hint="เก็บเกี่ยว"),
        )

        labels = [item.action.label for item in _message(reply_message).quick_reply.items]
        assert labels == ["บันทึกการเก็บเกี่ยว"]

    async def test_an_ambiguous_hint_shows_everything(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Two candidates: guessing would silently hide the other one."""
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        await seed_task_form(db_session, title="บันทึกการเก็บเกี่ยว")
        await seed_task_form(db_session, title="บันทึกกิจกรรมในแปลง")

        reply_message = await _send(
            client,
            line_user_id=line_user_id,
            text_content="อยากบันทึก",
            classify=_verdict(intent.Intent.SHOW_TASKS, task_hint="บันทึก"),
        )

        labels = [item.action.label for item in _message(reply_message).quick_reply.items]
        assert len(labels) == 2

    async def test_no_pending_tasks_says_so(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        reply_message = await _send(
            client,
            line_user_id=line_user_id,
            text_content="มีงานอะไรบ้าง",
            classify=_verdict(intent.Intent.SHOW_TASKS),
        )

        assert _message(reply_message).text == "ตอนนี้ยังไม่มีงานที่ต้องบันทึกครับ ขอบคุณมากนะครับ"


class TestNonTaskIntentsAnswerWithFixedText:
    async def _reply_for(
        self, db_session: AsyncSession, client: AsyncClient, verdict: intent.Intent
    ) -> str:
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        reply_message = await _send(
            client, line_user_id=line_user_id, text_content="อะไรสักอย่าง", classify=_verdict(verdict)
        )
        message = _message(reply_message)
        # Every non-task reply carries the one-tap way back to the task list.
        assert [item.action.text for item in message.quick_reply.items] == ["เริ่ม"]
        return str(message.text)

    async def test_greeting(self, db_session: AsyncSession, client: AsyncClient) -> None:
        assert "สวัสดีครับ" in await self._reply_for(db_session, client, intent.Intent.GREETING)

    async def test_help(self, db_session: AsyncSession, client: AsyncClient) -> None:
        assert "พักไว้ก่อน" in await self._reply_for(db_session, client, intent.Intent.HELP)

    async def test_off_topic_declines_instead_of_answering(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Weather/price/agronomy questions must never get an answer -- a
        wrong one here reaches a real crop.
        """
        reply = await self._reply_for(db_session, client, intent.Intent.OFF_TOPIC)
        assert "ช่วยได้เฉพาะการบันทึกข้อมูลแปลง" in reply

    async def test_unknown_falls_back_to_the_old_hint(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """Also what every classifier failure returns, so this is the
        pre-US2-11 behaviour, unchanged.
        """
        reply = await self._reply_for(db_session, client, intent.Intent.UNKNOWN)
        assert reply.startswith('รบกวนพิมพ์ "เริ่ม"')


class TestTheClassifierIsNotAlwaysCalled:
    async def test_exact_keyword_never_reaches_the_classifier(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The keyword path must stay free and instant -- and keep working
        while the provider is down.
        """
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        await seed_task_form(db_session, title="บันทึกการเก็บเกี่ยว")

        classify = _verdict(intent.Intent.OFF_TOPIC)
        reply_message = await _send(
            client, line_user_id=line_user_id, text_content="เริ่ม", classify=classify
        )

        classify.assert_not_awaited()
        assert _message(reply_message).text == "เลือกงานที่ต้องการทำ:"

    async def test_text_during_an_active_conversation_is_an_answer_not_a_command(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The rule that protects a farmer's real answers: mid-form text is
        never classified, even when it reads like a command.
        """
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        task_id, task_form_id = await seed_task_form(db_session)
        question_id = await seed_question(
            db_session,
            task_id=task_id,
            field_name="notes",
            input_type="VARCHAR",
            label="หมายเหตุ",
        )
        await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=question_id,
            status="active",
        )
        form_response = build_form_response(
            task_form_id=task_form_id,
            questions=[
                question_json(
                    question_id=question_id,
                    field_name="notes",
                    input_type="VARCHAR",
                    label="หมายเหตุ",
                )
            ],
        )

        classify = _verdict(intent.Intent.SHOW_TASKS)
        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            await _send(
                client,
                line_user_id=line_user_id,
                text_content="ขอทำฟอร์มใหม่",
                classify=classify,
            )

        classify.assert_not_awaited()
        row = await db_session.execute(
            text("SELECT answer->>'text' FROM chat.conversation_answer WHERE question_id = :qid"),
            {"qid": question_id},
        )
        assert row.scalar_one() == "ขอทำฟอร์มใหม่"
