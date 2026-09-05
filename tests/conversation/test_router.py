import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from src.conversation import service
from src.conversation.constants import ActiveSubstate
from src.database import get_session
from src.forms.schemas import FormDetail
from src.main import app


@pytest.fixture(autouse=True)
def _override_get_session() -> AsyncGenerator[None, None]:
    # The dev router's routes all depend on get_session for a real DB
    # session -- none of these tests need a real Postgres, since
    # dev_queries/service are patched below, so a dummy session is enough
    # to satisfy the dependency.
    async def _fake_session() -> AsyncGenerator[None, None]:
        yield None

    app.dependency_overrides[get_session] = _fake_session
    yield
    app.dependency_overrides.pop(get_session, None)


_TASK_FORM_ID = uuid.uuid4()
_TASK_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()
_CONVERSATION_ID = uuid.uuid4()


async def test_list_mock_forms_returns_dev_queries_result(client: AsyncClient) -> None:
    from src.conversation.schemas import FormSummary

    forms = [
        FormSummary(task_id=_TASK_ID, task_form_id=_TASK_FORM_ID, title="งาน", handler="notes")
    ]
    with patch(
        "src.conversation.router.dev_queries.list_testable_forms",
        new=AsyncMock(return_value=forms),
    ):
        response = await client.get("/conversation/test/forms")

    assert response.status_code == 200
    assert response.json()[0]["title"] == "งาน"


async def test_start_returns_the_first_question(client: AsyncClient) -> None:
    reply = service.ConversationReply(
        conversation_id=_CONVERSATION_ID,
        substate=ActiveSubstate.GUIDED_ASKING_FIXED_QUESTION,
        text="คำถามแรก",
        input_type="VARCHAR",
    )
    with (
        patch(
            "src.conversation.router.dev_queries.resolve_test_user_id",
            new=AsyncMock(return_value=_USER_ID),
        ),
        patch(
            "src.conversation.router.dev_queries.load_form_detail",
            new=AsyncMock(return_value=FormDetail(task_form_id=str(_TASK_FORM_ID))),
        ),
        patch(
            "src.conversation.router.service.start_conversation", new=AsyncMock(return_value=reply)
        ),
    ):
        response = await client.post(
            "/conversation/test/start",
            json={"task_id": str(_TASK_ID), "task_form_id": str(_TASK_FORM_ID)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "คำถามแรก"
    assert body["input_type"] == "VARCHAR"


async def test_message_returns_409_when_an_answer_is_already_in_flight(
    client: AsyncClient,
) -> None:
    with (
        patch(
            "src.conversation.router.dev_queries.load_form_detail",
            new=AsyncMock(return_value=FormDetail(task_form_id=str(_TASK_FORM_ID))),
        ),
        patch("src.conversation.router.service.handle_answer", new=AsyncMock(return_value=None)),
    ):
        response = await client.post(
            "/conversation/test/message",
            json={
                "conversation_id": str(_CONVERSATION_ID),
                "task_form_id": str(_TASK_FORM_ID),
                "text": "5",
            },
        )

    assert response.status_code == 409


async def test_ui_serves_the_chat_test_template(client: AsyncClient) -> None:
    response = await client.get("/conversation/test/ui")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
