from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

# matches tests/conftest.py's CRON_SECRET
_SECRET = "test-cron-secret"


async def test_missing_secret_is_rejected(client: AsyncClient) -> None:
    resp = await client.get("/internal/cron/reminders")
    assert resp.status_code == 401


async def test_wrong_secret_is_rejected(client: AsyncClient) -> None:
    resp = await client.get(
        "/internal/cron/reminders", headers={"Authorization": "Bearer not-the-secret"}
    )
    assert resp.status_code == 401


async def test_missing_bearer_prefix_is_rejected(client: AsyncClient) -> None:
    # The header must be exactly "Bearer <secret>" -- Vercel Cron's own
    # header shape -- not the bare secret.
    resp = await client.get("/internal/cron/reminders", headers={"Authorization": _SECRET})
    assert resp.status_code == 401


async def test_correct_secret_runs_the_reminder_job(client: AsyncClient) -> None:
    with patch("src.internal.router.check_and_send_reminders", new=AsyncMock()) as run_reminders:
        resp = await client.get(
            "/internal/cron/reminders", headers={"Authorization": f"Bearer {_SECRET}"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    run_reminders.assert_awaited_once()


async def test_correct_secret_runs_the_pause_idle_job(client: AsyncClient) -> None:
    with patch("src.internal.router.pause_idle_conversations", new=AsyncMock()) as run_pause:
        resp = await client.get(
            "/internal/cron/pause-idle-conversations",
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    run_pause.assert_awaited_once()
