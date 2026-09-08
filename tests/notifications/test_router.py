from unittest.mock import AsyncMock, patch
from uuid import uuid4

from httpx import AsyncClient

from src.database import get_session
from src.main import app
from src.notifications.schemas import SendNotificationResponse

# matches tests/conftest.py's CHATBOT_SERVICE_KEY
_KEY = "test-service-key"


async def _no_db_session():
    yield None


def setup_function() -> None:
    app.dependency_overrides[get_session] = _no_db_session


def teardown_function() -> None:
    app.dependency_overrides.clear()


async def test_missing_service_key_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/service/notifications",
        json={"user_ids": [str(uuid4())], "message": "hi"},
    )
    assert resp.status_code == 401


async def test_wrong_service_key_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/service/notifications",
        json={"user_ids": [str(uuid4())], "message": "hi"},
        headers={"X-Service-Key": "not-the-key"},
    )
    assert resp.status_code == 401


async def test_empty_user_ids_is_a_validation_error(client: AsyncClient) -> None:
    resp = await client.post(
        "/service/notifications",
        json={"user_ids": [], "message": "hi"},
        headers={"X-Service-Key": _KEY},
    )
    assert resp.status_code == 422


async def test_valid_request_returns_sent_and_failed(client: AsyncClient) -> None:
    uid, missing = uuid4(), uuid4()
    fake = SendNotificationResponse(
        sent=[uid],
        failed=[{"user_id": missing, "reason": "no_line_identity"}],
    )
    with patch(
        "src.notifications.router.send_notification",
        new=AsyncMock(return_value=fake),
    ) as send:
        resp = await client.post(
            "/service/notifications",
            json={"user_ids": [str(uid), str(missing)], "message": "ประชุมพรุ่งนี้"},
            headers={"X-Service-Key": _KEY},
        )
    assert resp.status_code == 200
    send.assert_awaited_once()
    body = resp.json()
    assert body["sent"] == [str(uid)]
    assert body["failed"] == [{"user_id": str(missing), "reason": "no_line_identity"}]
