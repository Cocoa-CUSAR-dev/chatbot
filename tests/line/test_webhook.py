from httpx import AsyncClient


async def test_webhook_rejects_bad_signature(client: AsyncClient) -> None:
    response = await client.post(
        "/line/webhook",
        content=b'{"events": []}',
        headers={"X-Line-Signature": "not-a-real-signature"},
    )
    assert response.status_code == 400
