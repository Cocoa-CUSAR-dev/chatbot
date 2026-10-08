"""GEODATA answered through LINE's own location picker, against a real
Postgres session.

The thing worth a real round-trip here is the hand-off nothing else checks
end to end: a LOCATION webhook event (not text) has to end up as the exact
value shape mobile-backend's isValidGeodata accepts -- a non-empty list of
{"lat": number, "lng": number}. The chatbot's own validator reads "lat,lng"
strings, so the easy mistake is submitting that string; Go's validation gate
would reject the whole submission. _assert_go_accepts below is a line-for-
line port of isValidGeodata so the assertion is Go's rule, not ours.
"""

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
from tests.integration.helpers import (
    build_form_response,
    build_postback_event,
    question_json,
    seed_conversation,
    seed_conversation_answer,
    seed_question,
    seed_task_form,
    seed_user_with_line_identity,
    sign_signature,
)

# The V16 rule exactly as Kotlin sends it (camelCase); get_form() snake_cases
# it on the way in, which is the path this test exercises.
_GIS_RULE = {"type": "GEODATA", "validLatLng": True, "errorMessage": "พิกัดไม่ถูกต้อง"}


def _build_location_event(
    *, line_user_id: str, latitude: float, longitude: float, address: str | None
) -> bytes:
    """The shape LINE delivers when a farmer taps "Share" in its location
    sheet. Kept local rather than in helpers.py: no other test sends one.
    """
    message: dict[str, Any] = {
        "type": "location",
        "id": str(uuid.uuid4().int)[:18],
        "title": "ตำแหน่งที่ส่ง",
        "latitude": latitude,
        "longitude": longitude,
    }
    if address is not None:
        message["address"] = address
    payload = {
        "destination": "Udestination0000000000000000000",
        "events": [
            {
                "type": "message",
                "source": {"type": "user", "userId": line_user_id},
                "timestamp": 1700000000000,
                "mode": "active",
                "webhookEventId": str(uuid.uuid4()),
                "deliveryContext": {"isRedelivery": False},
                "replyToken": str(uuid.uuid4()),
                "message": message,
            }
        ],
    }
    return json.dumps(payload).encode()


async def _post(client: AsyncClient, body: bytes) -> AsyncMock:
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


def _sent(reply_message: AsyncMock) -> Any:
    return reply_message.await_args.args[0].messages[0]


def _assert_go_accepts(value: Any) -> None:
    """mobile-backend internal/validation/answer_validator.go isValidGeodata:
    a non-empty []interface{}, every element a map with numeric (or numeric-
    string) lat and lng. Ported rather than paraphrased, after a JSON round
    trip, because Go sees the decoded JSON, not the Python objects.
    """
    decoded = json.loads(json.dumps(value))
    assert isinstance(decoded, list) and len(decoded) > 0, f"not a non-empty list: {decoded!r}"
    for point in decoded:
        assert isinstance(point, dict), f"point is not an object: {point!r}"
        for key in ("lat", "lng"):
            assert isinstance(point.get(key), int | float), f"{key} is not a number: {point!r}"


async def _farm_activity_form(
    db_session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]]:
    """The real farm_activity shape, trimmed: a free-text question, then the
    optional "ตำแหน่งปัจจุบัน" GEODATA question (field_name "gis") last.
    """
    task_id, task_form_id = await seed_task_form(db_session, handler="farm_activity")
    notes_q = await seed_question(
        db_session, task_id=task_id, field_name="notes", input_type="VARCHAR", sort_order=1
    )
    gis_q = await seed_question(
        db_session,
        task_id=task_id,
        field_name="gis",
        input_type="GEODATA",
        label="ตำแหน่งปัจจุบัน",
        is_mandatory=False,
        sort_order=2,
    )
    form_response = build_form_response(
        task_form_id=task_form_id,
        questions=[
            question_json(
                question_id=notes_q,
                field_name="notes",
                input_type="VARCHAR",
                label="หมายเหตุ",
                sort_order=1,
            ),
            question_json(
                question_id=gis_q,
                field_name="gis",
                input_type="GEODATA",
                label="ตำแหน่งปัจจุบัน",
                is_mandatory=False,
                sort_order=2,
                validation_rule=_GIS_RULE,
            ),
        ],
    )
    return task_id, task_form_id, notes_q, gis_q, form_response


async def _stored_answer(db_session: AsyncSession, question_id: uuid.UUID) -> Any:
    row = await db_session.execute(
        text("SELECT answer FROM chat.conversation_answer WHERE question_id = :qid"),
        {"qid": question_id},
    )
    return row.scalar_one_or_none()


class TestLocationAnswersTheGeodataQuestion:
    async def test_shared_location_is_stored_in_gos_shape_and_submitted_as_such(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """The full flow: location event -> stored -> summary -> confirm ->
        the Go submit receives "gis" as [{"lat", "lng"}].
        """
        task_id, task_form_id, notes_q, gis_q, form_response = await _farm_activity_form(db_session)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=gis_q,
            status="active",
        )
        await seed_conversation_answer(
            db_session, conversation_id=conversation_id, question_id=notes_q, text_value="ใส่ปุ๋ย"
        )

        submit_task = AsyncMock()
        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            summary = await _post(
                client,
                _build_location_event(
                    line_user_id=line_user_id,
                    latitude=18.7883,
                    longitude=98.9853,
                    address="ต.สุเทพ อ.เมือง จ.เชียงใหม่",
                ),
            )
            stored = await _stored_answer(db_session, gis_q)
            with patch("src.conversation.service.submit_task", new=submit_task):
                await _post(
                    client,
                    build_postback_event(
                        line_user_id=line_user_id, data=f"confirm:{conversation_id}"
                    ),
                )

        assert stored["value"] == [{"lat": 18.7883, "lng": 98.9853}]
        assert stored["text"] == "📍 ต.สุเทพ อ.เมือง จ.เชียงใหม่"
        assert stored["source_kind"] == "line_location"
        # Last question answered -> the summary, showing the address.
        assert "📍 ต.สุเทพ อ.เมือง จ.เชียงใหม่" in _sent(summary).text

        submission = submit_task.await_args.args[0]
        assert submission.answer["gis"] == [{"lat": 18.7883, "lng": 98.9853}]
        _assert_go_accepts(submission.answer["gis"])
        assert submission.answer["notes"] == "ใส่ปุ๋ย"

    async def test_without_an_address_the_summary_shows_the_coordinates(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id, notes_q, gis_q, form_response = await _farm_activity_form(db_session)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        conversation_id = await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=gis_q,
            status="active",
        )
        await seed_conversation_answer(
            db_session, conversation_id=conversation_id, question_id=notes_q, text_value="x"
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            await _post(
                client,
                _build_location_event(
                    line_user_id=line_user_id, latitude=13.75, longitude=100.5, address=None
                ),
            )

        assert (await _stored_answer(db_session, gis_q))["text"] == "📍 13.750000, 100.500000"

    async def test_out_of_range_coordinates_are_rejected_with_the_rules_message(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        task_id, task_form_id, _, gis_q, form_response = await _farm_activity_form(db_session)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=gis_q,
            status="active",
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _post(
                client,
                _build_location_event(
                    line_user_id=line_user_id, latitude=123.0, longitude=100.5, address=None
                ),
            )

        assert "พิกัดไม่ถูกต้อง" in _sent(reply_message).text
        assert await _stored_answer(db_session, gis_q) is None


class TestLocationThatIsNotTheAnswerIsNeverStored:
    async def test_location_while_another_question_is_open(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        """A coordinate saved against a free-text question would be silently
        wrong data -- the farmer is shown the question they're actually on.
        """
        task_id, task_form_id, notes_q, gis_q, form_response = await _farm_activity_form(db_session)
        line_user_id = f"U{uuid.uuid4().hex}"
        user_id = await seed_user_with_line_identity(db_session, line_user_id=line_user_id)
        await seed_conversation(
            db_session,
            user_id=user_id,
            task_id=task_id,
            task_form_id=task_form_id,
            current_question_id=notes_q,
            status="active",
        )

        with respx.mock:
            respx.get(f"{forms_settings.KOTLIN_BACKEND_URL}/service/forms/{task_form_id}").mock(
                return_value=Response(200, json=form_response)
            )
            reply_message = await _post(
                client,
                _build_location_event(
                    line_user_id=line_user_id, latitude=13.75, longitude=100.5, address=None
                ),
            )

        sent = _sent(reply_message).text
        assert sent.startswith("ตอนนี้ยังไม่ได้ถามตำแหน่งครับ")
        assert "หมายเหตุ" in sent  # the open question, re-shown
        assert await _stored_answer(db_session, notes_q) is None
        assert await _stored_answer(db_session, gis_q) is None

    async def test_location_with_no_active_conversation(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        line_user_id = f"U{uuid.uuid4().hex}"
        await seed_user_with_line_identity(db_session, line_user_id=line_user_id)

        reply_message = await _post(
            client,
            _build_location_event(
                line_user_id=line_user_id, latitude=13.75, longitude=100.5, address=None
            ),
        )

        assert "เพื่อดูงานที่ต้องทำ" in _sent(reply_message).text
        count = await db_session.execute(text("SELECT COUNT(*) FROM chat.conversation_answer"))
        assert count.scalar_one() == 0

    async def test_unlinked_farmer_is_told_so(
        self, db_session: AsyncSession, client: AsyncClient
    ) -> None:
        reply_message = await _post(
            client,
            _build_location_event(
                line_user_id=f"U{uuid.uuid4().hex}", latitude=13.75, longitude=100.5, address=None
            ),
        )

        assert "ยังไม่ได้เชื่อมกับระบบ" in _sent(reply_message).text
