"""Diary Client -- calls Kotlin's POST /service/diaries/generate right
after a farmer's submission is confirmed, so today's diary reflects what
just landed (docs-and-plan#130, #132, #133). Same service-key trust model
as src/forms/client.py's get_form and src/tasks/client.py's submit_task.
"""

import httpx

from src.diary.config import diary_settings
from src.diary.exceptions import DiaryNotAvailable
from src.exceptions import UpstreamServiceError


async def generate_diary(user_id: str) -> str:
    # 60s, not the 30s src/forms/client.py's get_form and
    # src/tasks/client.py's submit_task use: this path does BOTH a cold-cache
    # DB round-trip per resolved reference field (BE-5's same shape, one
    # query per plot/fertilizer/activity answered today) AND a Gemini call,
    # not just one or the other. Confirmed live 2026-09-18: a real diary
    # with several answered forms took 33.8s end to end -- past the old 30s
    # ceiling despite succeeding.
    async with httpx.AsyncClient(
        base_url=diary_settings.KOTLIN_BACKEND_URL, timeout=60.0
    ) as client:
        response = await client.post(
            "/service/diaries/generate",
            json={"userId": user_id},
            headers={"X-Service-Key": diary_settings.KOTLIN_SERVICE_KEY},
        )

    if response.status_code == 404:
        raise DiaryNotAvailable(f"No diary could be generated for user_id={user_id}")
    if response.status_code == 401:
        raise UpstreamServiceError(
            "Kotlin rejected the service key (401) -- confirm KOTLIN_SERVICE_KEY here "
            "matches CHATBOT_SERVICE_KEY on web-backend"
        )
    if response.status_code >= 400:
        raise UpstreamServiceError(f"Kotlin backend returned {response.status_code}")

    body = response.json()
    error = body.get("error")
    if error:
        raise UpstreamServiceError(f"Kotlin returned an error envelope: {error}")

    diary_text: str = body["value"]
    return diary_text
