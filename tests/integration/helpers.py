"""Shared setup for webhook-level integration tests -- signed LINE payloads
and DB seed rows, kept in one place so each test scenario only states what's
different about it.
"""

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def sign_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def build_text_message_event(
    *, line_user_id: str, text_content: str, reply_token: str | None = None
) -> bytes:
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
                "replyToken": reply_token or str(uuid.uuid4()),
                "message": {
                    "type": "text",
                    "id": str(uuid.uuid4().int)[:20],
                    "text": text_content,
                    "quoteToken": str(uuid.uuid4()),
                },
            }
        ],
    }
    return json.dumps(payload).encode()


async def seed_user_with_line_identity(session: AsyncSession, *, line_user_id: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO auth.user_account (user_id) VALUES (:user_id)"),
        {"user_id": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO auth.line_identity (user_id, line_user_id) "
            "VALUES (:user_id, :line_user_id)"
        ),
        {"user_id": user_id, "line_user_id": line_user_id},
    )
    await session.commit()
    return user_id


async def seed_task_form(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    task_id = uuid.uuid4()
    task_form_id = uuid.uuid4()

    await session.execute(
        text("INSERT INTO form.task (task_id) VALUES (:task_id)"), {"task_id": task_id}
    )
    await session.execute(
        text("INSERT INTO form.task_form (form_id, task_id) VALUES (:form_id, :task_id)"),
        {"form_id": task_form_id, "task_id": task_id},
    )
    await session.commit()
    return task_id, task_form_id


async def seed_question(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    field_name: str,
    input_type: str,
    label: str = "คำถามทดสอบ",
    is_mandatory: bool = True,
    sort_order: int = 1,
) -> uuid.UUID:
    question_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO form.question "
            "(question_id, section_id, label, field_name, input_type, sort_order, is_mandatory) "
            "VALUES (:question_id, :section_id, :label, :field_name, :input_type, "
            ":sort_order, :is_mandatory)"
        ),
        {
            "question_id": question_id,
            "section_id": uuid.uuid4(),
            "label": label,
            "field_name": field_name,
            "input_type": input_type,
            "sort_order": sort_order,
            "is_mandatory": is_mandatory,
        },
    )
    await session.commit()
    return question_id


def question_json(
    *,
    question_id: uuid.UUID,
    field_name: str,
    input_type: str,
    label: str = "คำถามทดสอบ",
    is_mandatory: bool = True,
    sort_order: int = 1,
    validation_rule: dict[str, Any] | None = None,
    choices: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A question exactly as Kotlin's GET /service/forms/{formId} would spell
    it -- camelCase, matching web-backend's Question.Entity -- for respx to
    hand back before forms/client.py's get_form() converts it.

    `validation_rule`, if given, should use the same camelCase spelling
    form.field_validation_rule stores it with (e.g. "errorMessage", not
    "error_message") -- get_form()'s _convert_keys() converts it recursively,
    same as it would a real Kotlin response.

    `choices` (OPTION questions only -- BOOLEAN's are synthesized in
    service.py itself, never sent by Kotlin) is `[{"id": ..., "name": ...}]`,
    matching Kotlin's own field names verbatim (no camelCase in either key).
    """
    question: dict[str, Any] = {
        "questionId": str(question_id),
        "label": label,
        "fieldName": field_name,
        "inputType": input_type,
        "isMandatory": is_mandatory,
        "sortOrder": sort_order,
    }
    if validation_rule is not None:
        question["validationRule"] = validation_rule
    if choices is not None:
        question["choices"] = choices
    return question


def build_form_response(
    *, task_form_id: uuid.UUID, questions: list[dict[str, Any]]
) -> dict[str, Any]:
    """The full `{"value": ..., "error": null}` envelope forms/client.py's
    get_form() expects, wrapping the given questions in a single section.
    """
    return {
        "value": {
            "formId": str(task_form_id),
            "sections": [{"questions": questions}],
        },
        "error": None,
    }


async def seed_active_conversation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    task_form_id: uuid.UUID,
    current_question_id: uuid.UUID,
) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO chat.conversation "
            "(conversation_id, user_id, task_id, task_form_id, status, current_question_id) "
            "VALUES (:conversation_id, :user_id, :task_id, :task_form_id, 'active', "
            ":current_question_id)"
        ),
        {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "task_id": task_id,
            "task_form_id": task_form_id,
            "current_question_id": current_question_id,
        },
    )
    await session.commit()
    return conversation_id
