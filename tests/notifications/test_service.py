from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.notifications.service import send_notification


async def _send(id_map, user_ids, *, multicast=None):
    session = AsyncMock()
    multicast = multicast or AsyncMock()
    push = AsyncMock()
    with (
        patch(
            "src.notifications.service.lookup_line_user_ids",
            new=AsyncMock(return_value=id_map),
        ),
        patch("src.notifications.service.push_text", new=push),
        patch("src.notifications.service.multicast_text", new=multicast),
    ):
        result = await send_notification(session, user_ids, "ประชุมพรุ่งนี้ 9 โมง")
    return result, push, multicast


async def test_single_recipient_uses_push_not_multicast():
    u = uuid4()
    result, push, multicast = await _send({u: "U1"}, [u])
    push.assert_awaited_once()
    multicast.assert_not_awaited()
    assert result.sent == [u]
    assert result.failed == []


async def test_multiple_recipients_use_multicast():
    a, b = uuid4(), uuid4()
    result, push, multicast = await _send({a: "U1", b: "U2"}, [a, b])
    multicast.assert_awaited_once()
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
    boom = AsyncMock(side_effect=RuntimeError("LINE 500"))
    result, _, _ = await _send({a: "U1", b: "U2"}, [a, b], multicast=boom)
    assert result.sent == []
    assert {f.user_id for f in result.failed} == {a, b}
    assert all(f.reason == "line_api_error" for f in result.failed)


async def test_nobody_reachable_sends_nothing():
    a = uuid4()
    result, push, multicast = await _send({}, [a])
    push.assert_not_awaited()
    multicast.assert_not_awaited()
    assert result.sent == []
    assert result.failed[0].reason == "no_line_identity"
