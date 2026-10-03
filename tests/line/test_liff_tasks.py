"""docs-and-plan#176: the chatbot's LIFF to-do list.

Lower-layer conversation logic (find_resumable_conversation,
resume_conversation, start_conversation) is already covered by
tests/conversation/test_service.py -- these tests are about the wiring on
top: auth resolution, "pending" filtering, and that each branch of
start_task pushes the right thing (or nothing, correctly, for the
empty-parent-choices case) rather than re-proving those primitives.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from src.conversation.constants import ActiveSubstate
from src.conversation.service import Choice, ConversationReply
from src.database import get_session
from src.exceptions import UpstreamServiceError
from src.line import liff_tasks
from src.line.config import line_settings
from src.line.exceptions import LineAccountNotLinked
from src.line.liff import InvalidLiffToken
from src.main import app
from src.tasks.schemas import TaskListItem

# Shaped like a real LIFF ID ("{channelId}-{randomId}", LINE's own format)
# so _channel_id_from_liff_id has something valid to split on. Scoped to
# this module (not conftest.py's global env vars) -- test_config.py asserts
# the real, unconfigured default is "".
_TEST_LIFF_ID = "1234567890-abcdefgh"


async def _no_db_session():
    yield None


@pytest.fixture(autouse=True)
def _liff_id_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(line_settings, "LIFF_ID", _TEST_LIFF_ID)


def setup_function() -> None:
    app.dependency_overrides[get_session] = _no_db_session


def teardown_function() -> None:
    app.dependency_overrides.clear()


# --- resolve_liff_user -------------------------------------------------


async def test_resolve_liff_user_rejects_missing_authorization() -> None:
    with pytest.raises(InvalidLiffToken):
        await liff_tasks.resolve_liff_user(session=MagicMock(), authorization=None)


async def test_resolve_liff_user_rejects_non_bearer_authorization() -> None:
    with pytest.raises(InvalidLiffToken):
        await liff_tasks.resolve_liff_user(session=MagicMock(), authorization="Basic xyz")


async def test_resolve_liff_user_raises_not_linked_when_no_identity_row() -> None:
    with (
        patch(
            "src.line.liff_tasks.liff.verify_id_token",
            AsyncMock(return_value={"sub": "Uabc"}),
        ),
        patch("src.line.liff_tasks.identity.lookup_user_id", AsyncMock(return_value=None)),
        pytest.raises(LineAccountNotLinked),
    ):
        await liff_tasks.resolve_liff_user(session=MagicMock(), authorization="Bearer tok")


async def test_resolve_liff_user_returns_resolved_user_on_success() -> None:
    user_id = uuid4()
    with (
        patch(
            "src.line.liff_tasks.liff.verify_id_token",
            AsyncMock(return_value={"sub": "Uabc"}),
        ),
        patch("src.line.liff_tasks.identity.lookup_user_id", AsyncMock(return_value=user_id)),
    ):
        result = await liff_tasks.resolve_liff_user(session=MagicMock(), authorization="Bearer tok")

    assert result.user_id == user_id
    assert result.line_user_id == "Uabc"


async def test_resolve_liff_user_derives_channel_id_from_liff_id() -> None:
    verify_mock = AsyncMock(return_value={"sub": "Uabc"})
    with (
        patch("src.line.liff_tasks.liff.verify_id_token", verify_mock),
        patch("src.line.liff_tasks.identity.lookup_user_id", AsyncMock(return_value=uuid4())),
    ):
        await liff_tasks.resolve_liff_user(session=MagicMock(), authorization="Bearer tok")

    verify_mock.assert_awaited_once_with("tok", "1234567890")


# --- GET /line/liff/tasks -----------------------------------------------


def _task_item(task_id: UUID, handler: str, status: str) -> TaskListItem:
    return TaskListItem(
        task_id=task_id,
        task_form_id=uuid4(),
        title="test task",
        handler=handler,
        status=status,
    )


async def test_list_pending_tasks_filters_out_completed(client: AsyncClient) -> None:
    pending = _task_item(uuid4(), "farm_activity", "NOT_STARTED")
    completed = _task_item(uuid4(), "harvest", "COMPLETED")
    app.dependency_overrides[liff_tasks.resolve_liff_user] = lambda: liff_tasks.ResolvedLiffUser(
        user_id=uuid4(), line_user_id="Uabc"
    )

    with patch(
        "src.line.liff_tasks.fetch_pending_tasks",
        AsyncMock(return_value=[pending, completed]),
    ):
        resp = await client.get("/line/liff/tasks")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["task_id"] == str(pending.task_id)


async def test_list_pending_tasks_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/line/liff/tasks")
    assert resp.status_code == 400


# --- POST /line/liff/tasks/{task_id}/start -------------------------------


def _override_liff_user(user_id: UUID) -> None:
    app.dependency_overrides[liff_tasks.resolve_liff_user] = lambda: liff_tasks.ResolvedLiffUser(
        user_id=user_id, line_user_id="Uabc"
    )


async def test_start_task_resumes_an_existing_conversation(client: AsyncClient) -> None:
    user_id = uuid4()
    task_id = uuid4()
    conversation = MagicMock(task_form_id=uuid4())
    reply = ConversationReply(
        conversation_id=uuid4(), substate=ActiveSubstate.AWAITING_INPUT, text="recap"
    )
    _override_liff_user(user_id)

    with (
        patch(
            "src.line.liff_tasks.service.find_resumable_conversation",
            AsyncMock(return_value=conversation),
        ),
        patch("src.line.liff_tasks.get_form", AsyncMock(return_value=MagicMock())),
        patch(
            "src.line.liff_tasks.service.resume_conversation", AsyncMock(return_value=reply)
        ) as resume_mock,
        patch("src.line.liff_tasks.push_text", AsyncMock()) as push_text_mock,
    ):
        resp = await client.post(
            f"/line/liff/tasks/{task_id}/start",
            json={"task_form_id": str(uuid4()), "handler": "farm_activity"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"pushed": True}
    resume_mock.assert_awaited_once()
    push_text_mock.assert_awaited_once_with("Uabc", "recap")


async def test_start_task_starts_fresh_when_no_resumable_conversation(client: AsyncClient) -> None:
    user_id = uuid4()
    task_id = uuid4()
    task_form_id = uuid4()
    reply = ConversationReply(
        conversation_id=uuid4(), substate=ActiveSubstate.AWAITING_INPUT, text="คำถามแรก"
    )
    _override_liff_user(user_id)

    with (
        patch(
            "src.line.liff_tasks.service.find_resumable_conversation",
            AsyncMock(return_value=None),
        ),
        patch("src.line.liff_tasks.get_form", AsyncMock(return_value=MagicMock())),
        patch("src.line.liff_tasks.parent_picker.kind_for_handler", return_value=None),
        patch("src.line.liff_tasks.fetch_last_answer", AsyncMock(return_value=None)),
        patch(
            "src.line.liff_tasks.service.start_conversation", AsyncMock(return_value=reply)
        ) as start_mock,
        patch("src.line.liff_tasks.push_text", AsyncMock()) as push_text_mock,
    ):
        resp = await client.post(
            f"/line/liff/tasks/{task_id}/start",
            json={"task_form_id": str(task_form_id), "handler": "farm_activity"},
        )

    assert resp.status_code == 200
    start_mock.assert_awaited_once()
    push_text_mock.assert_awaited_once_with("Uabc", "คำถามแรก")


async def test_start_task_offers_autofill_instead_of_starting_blind(client: AsyncClient) -> None:
    user_id = uuid4()
    task_id = uuid4()
    task_form_id = uuid4()
    _override_liff_user(user_id)

    with (
        patch(
            "src.line.liff_tasks.service.find_resumable_conversation",
            AsyncMock(return_value=None),
        ),
        patch("src.line.liff_tasks.get_form", AsyncMock(return_value=MagicMock(sections=[]))),
        patch("src.line.liff_tasks.parent_picker.kind_for_handler", return_value=None),
        patch("src.line.liff_tasks.fetch_last_answer", AsyncMock(return_value={"note": "old"})),
        patch("src.line.liff_tasks.reuse.sanitize_for_autofill", AsyncMock(return_value={})),
        patch("src.line.liff_tasks.reuse.format_autofill_preview", return_value=""),
        patch("src.line.liff_tasks.service.start_conversation", AsyncMock()) as start_mock,
        patch("src.line.liff_tasks.push_autofill_offer", AsyncMock()) as offer_mock,
    ):
        resp = await client.post(
            f"/line/liff/tasks/{task_id}/start",
            json={"task_form_id": str(task_form_id), "handler": "farm_activity"},
        )

    assert resp.status_code == 200
    offer_mock.assert_awaited_once()
    start_mock.assert_not_called()


async def test_start_task_tells_farmer_when_parent_choices_are_empty(client: AsyncClient) -> None:
    user_id = uuid4()
    task_id = uuid4()
    task_form_id = uuid4()
    _override_liff_user(user_id)

    with (
        patch(
            "src.line.liff_tasks.service.find_resumable_conversation",
            AsyncMock(return_value=None),
        ),
        patch("src.line.liff_tasks.get_form", AsyncMock(return_value=MagicMock())),
        patch("src.line.liff_tasks.parent_picker.kind_for_handler", return_value="farm"),
        patch("src.line.liff_tasks.parent_picker.choices_for", AsyncMock(return_value=[])),
        patch("src.line.liff_tasks.parent_picker.EMPTY_PROMPT", {"farm": "ยังไม่มีแปลง"}),
        patch("src.line.liff_tasks.service.start_conversation", AsyncMock()) as start_mock,
        patch("src.line.liff_tasks.push_text", AsyncMock()) as push_text_mock,
    ):
        resp = await client.post(
            f"/line/liff/tasks/{task_id}/start",
            json={"task_form_id": str(task_form_id), "handler": "farm_activity"},
        )

    assert resp.status_code == 200
    push_text_mock.assert_awaited_once_with("Uabc", "ยังไม่มีแปลง")
    start_mock.assert_not_called()


async def test_start_task_treats_fetch_last_answer_failure_as_no_offer(client: AsyncClient) -> None:
    # Same "never block a plain start over a Go hiccup" reasoning as
    # router.py's "start" postback branch.
    user_id = uuid4()
    task_id = uuid4()
    task_form_id = uuid4()
    reply = ConversationReply(
        conversation_id=uuid4(), substate=ActiveSubstate.AWAITING_INPUT, text="คำถามแรก"
    )
    _override_liff_user(user_id)

    with (
        patch(
            "src.line.liff_tasks.service.find_resumable_conversation",
            AsyncMock(return_value=None),
        ),
        patch("src.line.liff_tasks.get_form", AsyncMock(return_value=MagicMock())),
        patch("src.line.liff_tasks.parent_picker.kind_for_handler", return_value=None),
        patch(
            "src.line.liff_tasks.fetch_last_answer",
            AsyncMock(side_effect=UpstreamServiceError("Go is down")),
        ),
        patch(
            "src.line.liff_tasks.service.start_conversation", AsyncMock(return_value=reply)
        ) as start_mock,
        patch("src.line.liff_tasks.push_autofill_offer", AsyncMock()) as offer_mock,
        patch("src.line.liff_tasks.push_text", AsyncMock()),
    ):
        resp = await client.post(
            f"/line/liff/tasks/{task_id}/start",
            json={"task_form_id": str(task_form_id), "handler": "farm_activity"},
        )

    assert resp.status_code == 200
    start_mock.assert_awaited_once()
    offer_mock.assert_not_called()


# --- _push dispatch --------------------------------------------------------


async def test_push_dispatches_confirm_prompt_for_awaiting_confirmation() -> None:
    reply = ConversationReply(
        conversation_id=uuid4(),
        substate=ActiveSubstate.AWAITING_CONFIRMATION,
        text="ยืนยันไหม?",
    )
    with patch("src.line.liff_tasks.push_confirm_prompt", AsyncMock()) as mock:
        await liff_tasks._push("Uabc", reply)

    mock.assert_awaited_once_with("Uabc", "ยืนยันไหม?", reply.conversation_id)


async def test_push_dispatches_quick_reply_choices() -> None:
    reply = ConversationReply(
        conversation_id=uuid4(),
        substate=ActiveSubstate.AWAITING_INPUT,
        text="เลือกคำตอบ",
        choices=[Choice(id="1", label="ใช่"), Choice(id="2", label="ไม่")],
    )
    with patch("src.line.liff_tasks.push_text", AsyncMock()) as mock:
        await liff_tasks._push("Uabc", reply)

    args, kwargs = mock.await_args
    assert args[:2] == ("Uabc", "เลือกคำตอบ")
    quick_reply = kwargs["quick_reply"] if "quick_reply" in kwargs else args[2]
    assert [opt.label for opt in quick_reply] == ["ใช่", "ไม่"]


async def test_push_dispatches_plain_text() -> None:
    reply = ConversationReply(
        conversation_id=uuid4(), substate=ActiveSubstate.AWAITING_INPUT, text="พิมพ์คำตอบ"
    )
    with patch("src.line.liff_tasks.push_text", AsyncMock()) as mock:
        await liff_tasks._push("Uabc", reply)

    mock.assert_awaited_once_with("Uabc", "พิมพ์คำตอบ")
