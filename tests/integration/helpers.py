"""Shared setup for webhook-level integration tests -- signed LINE payloads
and DB seed rows, kept in one place so each test scenario only states what's
different about it.
"""

import base64
import hashlib
import hmac
import json
import uuid

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


async def seed_question(
    session: AsyncSession,
    *,
    field_name: str,
    input_type: str,
    label: str = "คำถามทดสอบ",
    is_mandatory: bool = True,
    sort_order: int = 1,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    task_id = uuid.uuid4()
    task_form_id = uuid.uuid4()
    question_id = uuid.uuid4()

    await session.execute(
        text("INSERT INTO form.task (task_id) VALUES (:task_id)"), {"task_id": task_id}
    )
    await session.execute(
        text("INSERT INTO form.task_form (form_id, task_id) VALUES (:form_id, :task_id)"),
        {"form_id": task_form_id, "task_id": task_id},
    )
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
    return task_id, task_form_id, question_id


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
