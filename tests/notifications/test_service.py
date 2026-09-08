from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.notifications.service import send_notification


async def _send(id_map, user_ids, *, multicast_batched=None):
    """multicast_batched, if given, should be an AsyncMock whose return
    value is (succeeded, failed) -- matching multicast_text_batched's real
    contract -- not a side_effect that raises, since the real function
    never lets a delivery error escape (see src/line/service.py).
    """
    session = AsyncMock()
    multicast_batched = multicast_batched or AsyncMock(side_effect=lambda to, _msg: (to, []))
    push = AsyncMock()
    with (
        patch(
            "src.notifications.service.lookup_line_user_ids",
            new=AsyncMock(return_value=id_map),
        ),
        patch("src.notifications.service.push_text", new=push),
        patch("src.notifications.service.multicast_text_batched", new=multicast_batched),
    ):
        result = await send_notification(session, user_ids, "ประชุมพรุ่งนี้ 9 โมง")
    return result, push, multicast_batched


async def test_single_recipient_uses_push_not_multicast():
    u = uuid4()
    result, push, multicast = await _send({u: "U1"}, [u])
    push.assert_awaited_once()
    multicast.assert_not_awaited()
    assert result.sent == [u]
    assert result.failed == []


async def test_multiple_recipients_use_multicast():
    a, b = uuid4(), uuid4()
    result, push, multicast_batched = await _send({a: "U1", b: "U2"}, [a, b])
    multicast_batched.assert_awaited_once()
    push.assert_not_awaited()
    assert set(result.sent) == {a, b}


async def test_users_without_a_line_account_are_reported_not_fatal():
    a, b = uuid4(), uuid4()
    result, _, _ = await _send({a: "U1"}, [a, b])
    assert result.sent == [a]
    assert [f.user_id for f in result.failed] == [b]
    assert result.failed[0].reason == "no_line_identity"


async def test_duplicate_user_ids_collapse():
    a = uuid4()
    result, push, _ = await _send({a: "U1"}, [a, a, a])
    push.assert_awaited_once()
    assert result.sent == [a]


async def test_line_delivery_error_marks_every_resolved_recipient_failed():
    a, b = uuid4(), uuid4()
    all_failed = AsyncMock(return_value=([], ["U1", "U2"]))
    result, _, _ = await _send({a: "U1", b: "U2"}, [a, b], multicast_batched=all_failed)
    assert result.sent == []
    assert {f.user_id for f in result.failed} == {a, b}
    assert all(f.reason == "line_api_error" for f in result.failed)


async def test_a_partially_failed_multicast_still_reports_the_recipients_that_succeeded():
    """The bug the review found: a later chunk failing used to mark
    everyone -- including already-delivered earlier recipients -- as
    failed. Now each recipient is reported by what actually happened to
    them.
    """
    a, b = uuid4(), uuid4()
    partial = AsyncMock(return_value=(["U1"], ["U2"]))
    result, _, _ = await _send({a: "U1", b: "U2"}, [a, b], multicast_batched=partial)
    assert result.sent == [a]
    assert [f.user_id for f in result.failed] == [b]
    assert result.failed[0].reason == "line_api_error"


async def test_nobody_reachable_sends_nothing():
    a = uuid4()
    result, push, multicast = await _send({}, [a])
    push.assert_not_awaited()
    multicast.assert_not_awaited()
    assert result.sent == []
    assert result.failed[0].reason == "no_line_identity"
