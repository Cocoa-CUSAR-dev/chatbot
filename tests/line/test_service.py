import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from src.line import service
from src.line.schemas import QuickReplyOption
from src.line.temp_task_picker import PendingTask


def _patched_messaging_api():
    messaging_api = MagicMock()
    messaging_api.reply_message = AsyncMock()
    messaging_api.push_message = AsyncMock()
    messaging_api.multicast = AsyncMock()
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return (
        patch("src.line.service.AsyncApiClient", return_value=client),
        patch("src.line.service.AsyncMessagingApi", return_value=messaging_api),
        messaging_api,
    )


async def test_reply_text_sends_a_text_message_with_quick_reply() -> None:
    ctx1, ctx2, messaging_api = _patched_messaging_api()
    with ctx1, ctx2:
        await service.reply_text(
            "reply-token", "เลือกคำตอบ", [QuickReplyOption(label="ใช่", text="ใช่")]
        )

    request = messaging_api.reply_message.await_args.args[0]
    assert request.reply_token == "reply-token"
    assert request.messages[0].text == "เลือกคำตอบ"
    assert request.messages[0].quick_reply.items[0].action.label == "ใช่"


async def test_reply_task_choices_labels_resumable_tasks_differently() -> None:
    ctx1, ctx2, messaging_api = _patched_messaging_api()
    task_id, task_form_id = uuid.uuid4(), uuid.uuid4()
    resumable = PendingTask(
        task_id=task_id, task_form_id=task_form_id, title="งานเก่า", handler="notes", has_conversation=True
    )

    with ctx1, ctx2:
        await service.reply_task_choices("reply-token", "เลือกงาน:", [resumable])

    action = messaging_api.reply_message.await_args.args[0].messages[0].quick_reply.items[0].action
    assert action.label == "🔄 งานเก่า"
    assert action.display_text == "ทำต่อ: งานเก่า"
    assert action.data == f"start:{task_id}:{task_form_id}:notes"


async def test_reply_confirm_prompt_offers_confirm_and_cancel() -> None:
    ctx1, ctx2, messaging_api = _patched_messaging_api()
    conversation_id = uuid.uuid4()

    with ctx1, ctx2:
        await service.reply_confirm_prompt("reply-token", "ยืนยันไหม?", conversation_id)

    items = messaging_api.reply_message.await_args.args[0].messages[0].quick_reply.items
    assert [item.action.data for item in items] == [
        f"confirm:{conversation_id}",
        f"cancel:{conversation_id}",
    ]


async def test_push_text_sends_a_push_message() -> None:
    ctx1, ctx2, messaging_api = _patched_messaging_api()

    with ctx1, ctx2:
        await service.push_text("Uabc123", "เตือนความจำ")

    request = messaging_api.push_message.await_args.args[0]
    assert request.to == "Uabc123"
    assert request.messages[0].text == "เตือนความจำ"


async def test_multicast_text_sends_to_every_recipient() -> None:
    ctx1, ctx2, messaging_api = _patched_messaging_api()

    with ctx1, ctx2:
        await service.multicast_text(["U1", "U2"], "แจ้งเตือน")

    request = messaging_api.multicast.await_args.args[0]
    assert request.to == ["U1", "U2"]
