"""GEODATA (location) questions in the guided flow.

They used to be filtered out of every form before the farmer ever saw them
(_SUPPORTED_INPUT_TYPES excluded GEODATA). Now they're asked, and answered
by tapping LINE's own location picker -- these tests pin the asking half;
answering is covered alongside handle_location.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from src.conversation import service
from src.forms.schemas import FormDetail
from src.line import router

_GIS_RULE = {"type": "GEODATA", "valid_lat_lng": True, "error_message": "พิกัดไม่ถูกต้อง"}


def _gis_question(*, is_mandatory: bool = False) -> dict[str, object]:
    """Matches the real seed: field_name "gis", optional, labelled
    "ตำแหน่งปัจจุบัน", with the V16 validation rule (already snake_cased the
    way forms/client.py's _convert_keys delivers it).
    """
    return {
        "question_id": str(uuid.uuid4()),
        "label": "ตำแหน่งปัจจุบัน",
        "field_name": "gis",
        "input_type": "GEODATA",
        "is_mandatory": is_mandatory,
        "sort_order": 0,
        "validation_rule": _GIS_RULE,
    }


def _form(*questions: dict[str, object]) -> FormDetail:
    return FormDetail(task_form_id="tf-gis", sections=[{"questions": list(questions)}])


class TestGeodataIsAsked:
    def test_geodata_question_is_no_longer_filtered_out(self) -> None:
        questions = service.questions_from_form(_form(_gis_question()))
        assert [q.field_name for q in questions] == ["gis"]

    def test_upload_is_still_filtered_out(self) -> None:
        """Photos stay out of scope -- only GEODATA was unblocked."""
        upload = {
            "question_id": str(uuid.uuid4()),
            "label": "รูปภาพ",
            "field_name": "upload",
            "input_type": "VARCHAR",
            "is_mandatory": False,
            "sort_order": 1,
        }
        questions = service.questions_from_form(_form(_gis_question(), upload))
        assert [q.field_name for q in questions] == ["gis"]

    def test_reply_carries_the_location_prompt_and_input_type(self) -> None:
        question = service.questions_from_form(_form(_gis_question()))[0]

        reply = service._reply_for_question(uuid.uuid4(), question)

        assert reply.text.startswith("ตำแหน่งปัจจุบัน")
        assert "📍 ส่งตำแหน่ง" in reply.text
        assert reply.input_type == "GEODATA"
        # Optional (every real GEODATA question is) -> skip and pause.
        assert [c.label for c in reply.choices or []] == ["⏭️ ข้าม", "⏸️ พักไว้ก่อน"]

    def test_other_question_types_get_no_location_prompt(self) -> None:
        varchar = {**_gis_question(), "input_type": "VARCHAR", "field_name": "notes"}
        question = service.questions_from_form(_form(varchar))[0]

        assert "📍" not in service._reply_for_question(uuid.uuid4(), question).text


class TestRouterAttachesTheLocationButton:
    async def test_geodata_reply_leads_with_a_location_action(self) -> None:
        question = service.questions_from_form(_form(_gis_question()))[0]
        reply = service._reply_for_question(uuid.uuid4(), question)

        with patch("src.line.router.reply_text", new=AsyncMock()) as reply_text:
            await router._reply("reply-token", reply)

        options = reply_text.await_args.kwargs["quick_reply"]
        assert [(o.kind, o.label) for o in options] == [
            ("location", "📍 ส่งตำแหน่ง"),
            ("message", "⏭️ ข้าม"),
            ("message", "⏸️ พักไว้ก่อน"),
        ]

    async def test_non_geodata_reply_is_unchanged(self) -> None:
        boolean = {**_gis_question(), "input_type": "BOOLEAN", "field_name": "is_ok"}
        question = service.questions_from_form(_form(boolean))[0]
        reply = service._reply_for_question(uuid.uuid4(), question)

        with patch("src.line.router.reply_text", new=AsyncMock()) as reply_text:
            await router._reply("reply-token", reply)

        options = reply_text.await_args.kwargs["quick_reply"]
        assert all(o.kind == "message" for o in options)


class TestEditAndResumeReShowTheLocationPrompt:
    async def test_editing_the_location_re_asks_with_the_button(self) -> None:
        """US2-6's แก้ไข re-opens the question through _reply_for_question, so
        the farmer gets the 📍 prompt (and input_type for the button) again.
        """
        form = _form(_gis_question())
        question = service.questions_from_form(form)[0]
        conversation = MagicMock(conversation_id=uuid.uuid4(), current_page=0)
        session = MagicMock()
        session.get = AsyncMock(return_value=conversation)
        session.commit = AsyncMock()

        # No answer rows: begin_edit checks them first, to refuse an edit
        # after part of a several-answer submission was saved (chatbot#76).
        with patch("src.conversation.service._answered_rows", new=AsyncMock(return_value=[])):
            reply = await service.begin_edit(
                session,
                conversation_id=conversation.conversation_id,
                question_id=question.question_id,
                form=form,
            )

        assert reply.input_type == "GEODATA"
        assert "กรุณากดปุ่ม 📍 ส่งตำแหน่ง ด้านล่างครับ" in reply.text

    async def test_resuming_at_the_location_question_re_asks_with_the_button(self) -> None:
        form = _form(_gis_question())
        question = service.questions_from_form(form)[0]
        conversation = MagicMock(
            conversation_id=uuid.uuid4(),
            current_question_id=question.question_id,
            current_page=0,
            parent_answer=None,
        )
        session = MagicMock()
        session.commit = AsyncMock()
        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=execute_result)

        reply = await service.resume_conversation(session, conversation=conversation, form=form)

        assert reply.input_type == "GEODATA"
        assert reply.text.startswith("ตำแหน่งปัจจุบัน")
        assert "กรุณากดปุ่ม 📍 ส่งตำแหน่ง ด้านล่างครับ" in reply.text
